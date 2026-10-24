"""连通性测试 MCP 服务。

只提供一个掷骰子工具，用来验证「灵矽平台 → 我们的 MCP 服务」整条链路，
并把平台携带的请求头打印出来，确认能拿到哪些会话/设备标识。
"""

import logging
import os
import random

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

log = logging.getLogger("linx_game.probe")

# 平台按文档会带这些头；声纹等未写进文档的信息也靠这里观察
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


class HeaderLogMiddleware:
    """打印每个请求的 x- 开头请求头，便于观察平台实际传了什么。"""

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
    app = mcp.sse_app(
        sse_path="/mcp",
        message_path="/mcp/message",
        host="0.0.0.0",
        # 平台从公网访问，Host 不固定，关闭 DNS rebinding 校验
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    uvicorn.run(HeaderLogMiddleware(app), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
