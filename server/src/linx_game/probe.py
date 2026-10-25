"""MCP 连通性验证服务。

提供 roll_dice 工具，并记录灵矽平台调用时注入的 X-Linx-* 请求头。
"""

import json
import logging
import os
import random

import httpx
import uvicorn
from dotenv import find_dotenv, load_dotenv
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent

from linx_game import llm_gateway

log = logging.getLogger("linx_game.probe")

# 灵矽平台调用云端 MCP 时注入的请求头
LINX_HEADERS = ("x-linx-device-id", "x-linx-agent-id", "x-linx-session-id")

mcp = MCPServer("linx-game-probe")


def _linx_headers(ctx: Context) -> dict[str, str]:
    request = ctx.request_context.request
    if request is None:
        return {}
    return {k: v for k, v in request.headers.items() if k.lower() in LINX_HEADERS}


@mcp.tool(description="掷一个骰子。用户说「掷骰子」「摇个点数」时调用。")
def roll_dice(ctx: Context, sides: int = 6) -> str:
    sides = max(2, min(sides, 100))
    value = random.randint(1, sides)
    headers = _linx_headers(ctx)
    log.info("roll_dice sides=%d value=%d headers=%s", sides, value, headers)
    return f"掷出了 {value} 点（{sides} 面骰）。"


@mcp.tool(
    description="记录一条玩家发言。用户要求「记录发言」时调用：speaker 填声纹识别出的当前说话人称呼，"
    "无法识别时填「未知」；content 填要记录的内容。"
)
def record_speech(ctx: Context, speaker: str, content: str) -> str:
    log.info("record_speech speaker=%s content=%s headers=%s", speaker, content, _linx_headers(ctx))
    return f"已记录 {speaker} 的发言。"


def _switch_targets() -> dict[str, str]:
    """解析 LINX_SWITCH_TARGETS，格式为「名称=智能体ID,名称=智能体ID」。"""
    pairs = (item.split("=", 1) for item in os.getenv("LINX_SWITCH_TARGETS", "").split(",") if "=" in item)
    return {name.strip(): agent_id.strip() for name, agent_id in pairs}


@mcp.tool(description="把当前设备切换给另一位嫌疑人。玩家要求换人谈话时调用，target 填对方名字。")
async def switch_suspect(ctx: Context, target: str) -> str:
    headers = _linx_headers(ctx)
    agent_id = _switch_targets().get(target)
    if agent_id is None:
        return f"没有名为 {target} 的嫌疑人。"
    device_id = headers.get("x-linx-device-id", "")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.put(
            f"{os.environ['LINX_BASE_URL']}/v1/devices/{device_id}/agent/{agent_id}",
            headers={"Authorization": f"Bearer {os.environ['LINX_API_KEY']}"},
            json={"disable_chat_history_migration": os.getenv("LINX_SWITCH_MIGRATE") != "1"},
        )
    log.info("switch_suspect target=%s agent=%s resp=%s headers=%s", target, agent_id, resp.text, headers)
    return f"{target} 即将进入房间。"


@mcp.tool(description="在当前会话内换另一位嫌疑人上场（change_role 方式）。玩家要求换人谈话时调用，target 填对方名字。")
async def change_suspect(ctx: Context, target: str) -> CallToolResult:
    agent_id = _switch_targets().get(target)
    if agent_id is None:
        return CallToolResult(content=[TextContent(type="text", text=f"没有名为 {target} 的嫌疑人。")])
    # 取目标智能体自身的角色设定，作为 change_role 的新提示词
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{os.environ['LINX_BASE_URL']}/xiaozhi/agent/{agent_id}",
            headers={"Authorization": f"Bearer {os.environ['LINX_API_KEY']}"},
        )
    result = {"code": 0, "message": "", "funcs": [
        {"name": "change_role", "arguments": {"role_prompt": resp.json()["data"]["systemPrompt"]}},
    ]}
    log.info("change_suspect target=%s headers=%s", target, _linx_headers(ctx))
    return CallToolResult(
        # 文本中不含新提示词，用于确认角色切换来自 change_role 而非模型读取工具结果
        content=[TextContent(type="text", text=f"{target} 已进入房间。")],
        structured_content=result,
    )


def _act(ctx: Context, action: str, **args) -> str:
    log.info("act %s %s device=%s", action, json.dumps(args, ensure_ascii=False),
             _linx_headers(ctx).get("x-linx-device-id"))
    return "已执行。"


@mcp.tool(description="移动到另一个房间。room：大厅、书房、温室、厨房。")
def move_to(ctx: Context, room: str) -> str:
    return _act(ctx, "move_to", room=room)


@mcp.tool(description="仔细查看所在房间里的某个物品或人。")
def examine(ctx: Context, target: str) -> str:
    return _act(ctx, "examine", target=target)


@mcp.tool(description="拿走所在房间里的某个物品。")
def take(ctx: Context, item: str) -> str:
    return _act(ctx, "take", item=item)


@mcp.tool(description="把随身物品藏到所在房间的某处。")
def hide(ctx: Context, item: str, place: str) -> str:
    return _act(ctx, "hide", item=item, place=place)


@mcp.tool(description="本回合不行动，留在原地观察。")
def wait(ctx: Context) -> str:
    return _act(ctx, "wait")


@mcp.tool(description="说一句话。你说出口的每一句话都必须通过此工具；volume：耳语、正常、大喊。")
def say(ctx: Context, text: str, volume: str = "正常") -> str:
    return _act(ctx, "say", text=text, volume=volume)


@mcp.tool(description="【开发期】当局势或设定让你无法理解、互相矛盾或不知如何行动时调用，issue 写明具体问题。调用后照常继续扮演。")
def report_issue(ctx: Context, issue: str) -> str:
    log.warning("report_issue %s device=%s", issue, _linx_headers(ctx).get("x-linx-device-id"))
    return "已记录，请继续扮演。"


class HeaderLogMiddleware:
    """记录每个 HTTP 请求中以 x- 开头的请求头。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {
                k.decode(): v.decode()
                for k, v in scope["headers"]
                if k.startswith(b"x-") and k != b"x-api-key"
            }
            log.info("%s %s %s", scope["method"], scope["path"], headers)
        await self.app(scope, receive, send)


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    # 只保留行动类工具，减少无关工具对模型选择的干扰
    if os.getenv("PROBE_ACTION_TOOLS_ONLY") == "1":
        for name in ("roll_dice", "record_speech", "switch_suspect", "change_suspect"):
            mcp.remove_tool(name)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # 每次请求独立处理，会话由平台的 X-Linx-Session-Id 标识
        stateless_http=True,
        host="0.0.0.0",
        # 平台从公网访问，Host 不固定，关闭 DNS rebinding 校验
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def router(scope, receive, send):
        # /llm 前缀交给大模型网关，其余（含 lifespan）交给 MCP 应用
        if scope["type"] == "http" and scope["path"].startswith("/llm/"):
            scope = dict(scope, path=scope["path"][len("/llm"):])
            return await llm_gateway.app(scope, receive, send)
        return await app(scope, receive, send)

    uvicorn.run(HeaderLogMiddleware(router), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
