"""MCP 连通性验证服务。

提供 roll_dice 工具，并记录灵矽平台调用时注入的 X-Linx-* 请求头。
"""

import logging
import os
import random

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # 每次请求独立处理，会话由平台的 X-Linx-Session-Id 标识
        stateless_http=True,
        host="0.0.0.0",
        # 平台从公网访问，Host 不固定，关闭 DNS rebinding 校验
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    uvicorn.run(HeaderLogMiddleware(app), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
