"""大模型网关。

对灵矽平台暴露 OpenAI 兼容的 /chat/completions 接口，转发到上游推理服务（默认 DeepSeek 官方 API，
LLM_UPSTREAM=qiniu 时为七牛 AI 大模型推理），并在转发时注入平台自定义模型无法配置的参数（例如关闭思考）。
实际使用的模型由 LLM_MODEL 指定，默认取上游的默认模型。

灵矽侧把本网关注册为自定义模型：base_url 指向 <服务地址>/llm/v1，
api_key 填 LLM_GATEWAY_TOKEN。网关只接受携带该口令的请求。
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Protocol

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

log = logging.getLogger("linx_game.llm_gateway")

# 按上游模型注入的请求参数
BODY_OVERRIDES = {
    "moonshotai/kimi-k3-free": {"thinking": {"type": "disabled"}},
    "deepseek-flash": {"thinking": {"type": "disabled"}},
}

# 上游推理服务：名称 → (base_url 环境变量, api_key 环境变量, 默认模型)
# 七牛免费模型的频率限制不足以支撑多个角色同时思考，默认使用 DeepSeek 官方 API
UPSTREAMS = {
    "deepseek": ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY", "deepseek-flash"),
    "qiniu": ("QINIU_AI_BASE_URL", "QINIU_AI_API_KEY", "moonshotai/kimi-k3-free"),
}


def _upstream_spec() -> tuple[str, str, str]:
    return UPSTREAMS[os.getenv("LLM_UPSTREAM", "deepseek")]


def upstream() -> tuple[str, str]:
    """当前上游的 (base_url, api_key)。"""
    base_var, key_var, _ = _upstream_spec()
    return os.environ[base_var], os.environ[key_var]


def prepare(body: dict) -> dict:
    """按配置设定模型名并注入该模型需要的参数。"""
    body["model"] = os.getenv("LLM_MODEL") or _upstream_spec()[2]
    body.update(BODY_OVERRIDES.get(body["model"], {}))
    return body


class Controller(Protocol):
    """游戏角色的上下文控制器，由运行时按设备注册。"""

    def build(self, messages: list[dict]) -> list[dict]:
        """以平台转发的消息为基础，返回实际发给模型的完整消息列表。"""

    def on_reply(self, thought: str, tool_calls: list[dict]) -> None:
        """模型的回复：正文（角色内心想法，不回传平台，不会被播报）与工具调用。"""

    def on_turn_done(self) -> None:
        """本轮结束：模型未调用工具，或工具结果已返回。"""


# 按设备 MAC 注册：controllers 中的设备由运行时组织上下文；
# silent_devices 中的设备（例如只做语音识别的玩家连接）不调用模型，直接返回空回复。
controllers: dict[str, Controller] = {}
silent_devices: set[str] = set()


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
    request = prepare({
        "model": body.get("model"), "max_tokens": 300, "stream": False,
        "messages": [{"role": "system", "content": REVIEW_PROMPT},
                     {"role": "user", "content": _transcript(body.get("messages", []))}],
    })
    base_url, api_key = upstream()
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
            resp = await client.post("/chat/completions", json=request,
                                     headers={"Authorization": f"Bearer {api_key}"})
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


# 上游推理服务有频率限制：限制同时进行的请求数，并对 429 按退避重试
_upstream_slots = asyncio.Semaphore(int(os.getenv("LLM_GATEWAY_CONCURRENCY", "8")))
RETRY_DELAYS_S = (1, 2, 4, 8)


async def _send_with_retry(client: httpx.AsyncClient, body: dict, api_key: str) -> httpx.Response:
    for delay in (*RETRY_DELAYS_S, None):
        resp = await client.send(
            client.build_request("POST", "/chat/completions", json=body,
                                 headers={"Authorization": f"Bearer {api_key}"}),
            stream=True,
        )
        if resp.status_code != 429 or delay is None:
            return resp
        await resp.aclose()
        log.warning("upstream 429, retry in %ss", delay)
        await asyncio.sleep(delay)


async def chat_completions(request: Request) -> Response:
    token = os.getenv("LLM_GATEWAY_TOKEN")
    if not token or request.headers.get("authorization") != f"Bearer {token}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    device = request.headers.get("x-linx-device-id", "").upper()
    if device in silent_devices:
        return _empty_reply(body)
    controller = controllers.get(device)
    if controller is not None:
        messages = body.get("messages", [])
        if dump_dir := os.getenv("LLM_GATEWAY_DUMP_DIR"):
            with open(os.path.join(dump_dir, f"platform_{device.replace(':', '')}.json"), "w") as f:
                json.dump(messages[-4:], f, ensure_ascii=False, indent=2)
        # 工具结果返回后平台会再请求一次模型；角色的行动已由工具完成，这一轮直接结束
        if is_tool_followup(messages):
            controller.on_turn_done()
            return _empty_reply(body)
        body["messages"] = controller.build(messages)
    prepare(body)
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

    base_url, api_key = upstream()
    client = httpx.AsyncClient(base_url=base_url, timeout=120)
    await _upstream_slots.acquire()
    try:
        upstream_resp = await _send_with_retry(client, body, api_key)
    except BaseException:
        _upstream_slots.release()
        await client.aclose()
        raise

    if upstream_resp.status_code >= 400:
        error = await upstream_resp.aread()
        _upstream_slots.release()
        await upstream_resp.aclose()
        await client.aclose()
        log.warning("upstream %s device=%s %s", upstream_resp.status_code, device, error[:500].decode(errors="replace"))
        if dump_dir := os.getenv("LLM_GATEWAY_DUMP_DIR"):
            with open(os.path.join(dump_dir, "last_error_request.json"), "w") as f:
                json.dump(body, f, ensure_ascii=False, indent=2)
        return Response(error, status_code=upstream_resp.status_code, media_type="application/json")

    async def relay():
        buffer = b""
        try:
            # 上游可能压缩响应，按解压后的内容转发与解析
            async for chunk in upstream_resp.aiter_bytes():
                buffer += chunk
                if controller is None:
                    yield chunk
            if controller is not None:
                # 正文是角色的内心想法，不回传平台，避免被播报
                thought, stripped = _strip_content(buffer)
                yield stripped
                controller.on_reply(thought, _tool_calls(buffer))
                if b'"tool_calls"' not in buffer:
                    controller.on_turn_done()
        finally:
            _upstream_slots.release()
            await upstream_resp.aclose()
            await client.aclose()
            log.info("reply device=%s %s", device, json.dumps(_reply_text(buffer), ensure_ascii=False))

    return StreamingResponse(
        relay(), status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type", "application/json"),
    )


def _sse_json(line: str) -> dict | None:
    """解析 SSE（或普通 JSON）响应中的一行；不是 JSON 对象时返回 None。"""
    payload = line.removeprefix("data:").strip()
    if not payload.startswith("{"):
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        log.warning("unparsable upstream line: %r", line[:200])
        return None


def is_tool_followup(messages: list[dict]) -> bool:
    """是否为工具结果返回后的二次请求。平台有时会在工具结果后追加一条空白的用户消息。"""
    for m in reversed(messages):
        if m.get("role") == "user" and not str(m.get("content") or "").strip():
            continue
        return m.get("role") == "tool"
    return False


def _strip_content(raw: bytes) -> tuple[str, bytes]:
    """清空 SSE 流（或普通 JSON）中的正文，返回 (正文, 改写后的响应)。"""
    thought, lines = [], []
    for line in raw.decode(errors="replace").split("\n"):
        data = _sse_json(line)
        if data is not None:
            for choice in data.get("choices") or []:
                msg = choice.get("delta") or choice.get("message") or {}
                if msg.get("content"):
                    thought.append(msg["content"])
                    msg["content"] = ""
            line = ("data: " if line.startswith("data:") else "") + json.dumps(data, ensure_ascii=False)
        lines.append(line)
    return "".join(thought).strip(), "\n".join(lines).encode()


def _tool_calls(raw: bytes) -> list[dict]:
    """从 SSE 流（或普通 JSON）中拼出完整的工具调用列表。"""
    calls: dict[int, dict] = {}
    for line in raw.decode(errors="replace").splitlines():
        data = _sse_json(line)
        if data is None:
            continue
        for choice in data.get("choices") or []:
            msg = choice.get("delta") or choice.get("message") or {}
            for i, delta in enumerate(msg.get("tool_calls") or []):
                call = calls.setdefault(delta.get("index", i), {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                call["id"] = delta.get("id") or call["id"]
                fn = delta.get("function") or {}
                call["function"]["name"] += fn.get("name") or ""
                call["function"]["arguments"] += fn.get("arguments") or ""
    return [calls[i] for i in sorted(calls)]


def _empty_reply(body: dict) -> Response:
    """不调用模型，返回一条空的助手回复。"""
    base = {"id": f"chatcmpl-{uuid.uuid4().hex}", "created": int(time.time()), "model": body.get("model")}
    if not body.get("stream"):
        return JSONResponse({**base, "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]})
    chunk = {**base, "object": "chat.completion.chunk", "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]}
    return Response(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", media_type="text/event-stream")


def _reply_text(raw: bytes) -> str:
    """从上游响应（SSE 流或普通 JSON）中拼出回复文本与工具调用，用于日志。"""
    parts = []
    for line in raw.decode(errors="replace").splitlines():
        data = _sse_json(line)
        if data is None:
            continue
        for choice in data.get("choices") or []:
            msg = choice.get("delta") or choice.get("message") or {}
            parts.append(msg.get("content") or "")
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                parts.append(f"[tool {fn.get('name') or ''}{fn.get('arguments') or ''}]")
    return "".join(parts)


app = Starlette(routes=[Route("/v1/chat/completions", chat_completions, methods=["POST"])])
