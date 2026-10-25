"""大模型网关。

对灵矽平台暴露 OpenAI 兼容的 /chat/completions 接口，转发到七牛 AI 大模型推理，
并在转发时注入平台自定义模型无法配置的参数（例如关闭思考）。

灵矽侧把本网关注册为自定义模型：base_url 指向 <服务地址>/llm/v1，
api_key 填 LLM_GATEWAY_TOKEN。网关只接受携带该口令的请求。
"""

import asyncio
import json
import logging
import os

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

log = logging.getLogger("linx_game.llm_gateway")

# 按上游模型注入的请求参数
BODY_OVERRIDES = {
    "moonshotai/kimi-k3-free": {"thinking": {"type": "disabled"}},
}


# 平台在智能体提示词前后追加的通用内容：语言声明、终端位置、输出格式限制、节日与时间。
# 游戏角色有自己的时空设定与输出格式，这些内容会干扰角色扮演，转发前移除。
PLATFORM_PREFIX = "默认回复语言是中文"
PLATFORM_SUFFIX_MARKER = "# 终端用户上下文"


def strip_platform_prompt(system: str) -> str:
    if os.getenv("LLM_GATEWAY_KEEP_PLATFORM_PROMPT") == "1":
        return system
    system = system.split(PLATFORM_SUFFIX_MARKER, 1)[0]
    return system.removeprefix(PLATFORM_PREFIX).strip()


# 开发期旁路审查：每次转发的同时，另请模型以审查员身份检查同一上下文，
# 找出角色可能无法理解或相互矛盾的信息。结果只写日志，不影响对话。
REVIEW_PROMPT = (
    "你是一款语音推理游戏的开发审查员，不参与扮演。下面是程序发给某个 AI 角色的完整上下文。"
    "角色设定和局势信息由程序生成，可能有错误。请只检查最后一条输入，指出以下问题：\n"
    "1. 出现角色设定或规则中不存在的地点、人物、物品；\n"
    "2. 时间倒退，或与之前的信息相矛盾；\n"
    "3. 指令含糊，角色难以判断该怎么做。\n"
    "每个问题一行，简短具体。没有问题只输出：无"
)
_review_tasks: set[asyncio.Task] = set()


def _transcript(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str) and content and m.get("role") in ("system", "user", "assistant"):
            lines.append(f"[{m['role']}]\n{content}")
    return "\n\n".join(lines)


async def _review(body: dict) -> None:
    request = {
        "model": body.get("model"), "max_tokens": 300, "stream": False,
        "messages": [{"role": "system", "content": REVIEW_PROMPT},
                     {"role": "user", "content": _transcript(body.get("messages", []))}],
        **BODY_OVERRIDES.get(body.get("model"), {}),
    }
    try:
        async with httpx.AsyncClient(base_url=os.environ["QINIU_AI_BASE_URL"], timeout=60) as client:
            resp = await client.post("/chat/completions", json=request,
                                     headers={"Authorization": f"Bearer {os.environ['QINIU_AI_API_KEY']}"})
        verdict = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        log.exception("dev-review failed")
        return
    if verdict and verdict != "无":
        last = next((m.get("content") for m in reversed(body.get("messages", [])) if m.get("role") == "user"), "")
        log.warning("dev-review input=%s issues=%s", json.dumps(str(last)[:200], ensure_ascii=False),
                    json.dumps(verdict, ensure_ascii=False))


def start_review(body: dict) -> None:
    if os.getenv("LLM_GATEWAY_DEV_REVIEW") != "1":
        return
    task = asyncio.create_task(_review(body))
    _review_tasks.add(task)
    task.add_done_callback(_review_tasks.discard)


async def chat_completions(request: Request) -> Response:
    token = os.getenv("LLM_GATEWAY_TOKEN")
    if not token or request.headers.get("authorization") != f"Bearer {token}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    body.update(BODY_OVERRIDES.get(body.get("model"), {}))
    for message in body.get("messages", []):
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            message["content"] = strip_platform_prompt(message["content"])
    system = next((m.get("content") for m in body.get("messages", []) if m.get("role") == "system"), "")
    log.info(
        "chat model=%s messages=%d tools=%s params=%s system=%s",
        body.get("model"), len(body.get("messages", [])),
        [t.get("function", {}).get("name") for t in body.get("tools") or []],
        json.dumps({k: v for k, v in body.items() if k not in ("messages", "tools")}, ensure_ascii=False),
        json.dumps(str(system)[:2000], ensure_ascii=False),
    )

    if dump_dir := os.getenv("LLM_GATEWAY_DUMP_DIR"):
        # 调试用：保存完整请求体，核对平台转发的消息与工具定义
        with open(os.path.join(dump_dir, "last_request.json"), "w") as f:
            json.dump(body, f, ensure_ascii=False, indent=2)
        with open(os.path.join(dump_dir, "last_headers.json"), "w") as f:
            json.dump({k: v for k, v in request.headers.items() if k != "authorization"}, f, indent=2)
    start_review(body)

    client = httpx.AsyncClient(base_url=os.environ["QINIU_AI_BASE_URL"], timeout=120)
    upstream = await client.send(
        client.build_request(
            "POST", "/chat/completions", json=body,
            headers={"Authorization": f"Bearer {os.environ['QINIU_AI_API_KEY']}"},
        ),
        stream=True,
    )

    async def relay():
        buffer = b""
        try:
            async for chunk in upstream.aiter_raw():
                buffer += chunk
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
            log.info("reply %s", json.dumps(_reply_text(buffer), ensure_ascii=False))

    return StreamingResponse(
        relay(), status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


def _reply_text(raw: bytes) -> str:
    """从上游响应（SSE 流或普通 JSON）中拼出回复文本与工具调用，用于日志。"""
    parts = []
    for line in raw.decode(errors="replace").splitlines():
        line = line.removeprefix("data: ").strip()
        if not line.startswith("{"):
            continue
        for choice in json.loads(line).get("choices") or []:
            msg = choice.get("delta") or choice.get("message") or {}
            parts.append(msg.get("content") or "")
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                parts.append(f"[tool {fn.get('name') or ''}{fn.get('arguments') or ''}]")
    return "".join(parts)


app = Starlette(routes=[Route("/v1/chat/completions", chat_completions, methods=["POST"])])
