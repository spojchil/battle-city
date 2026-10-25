"""游戏服务入口。

同一进程提供三部分，均挂在同一端口：
- /mcp：AI 角色的行动工具（灵矽云端 MCP，Streamable HTTP）
- /llm/v1：大模型网关（灵矽自定义模型）
- / 与 /ws：网页客户端与游戏会话
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import uvicorn
from dotenv import find_dotenv, load_dotenv
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from linx_game import llm_gateway
from linx_game.runtime import tools
from linx_game.runtime.game import Game, client_view

log = logging.getLogger("linx_game.app")

STATIC_DIR = Path(__file__).parent / "web" / "static"
REPO_ROOT = Path(__file__).resolve().parents[3]


class Server:
    def __init__(self, scenario_path: Path):
        self.scenario_path = scenario_path
        self.game: Game | None = None

    async def websocket(self, ws: WebSocket) -> None:
        await ws.accept()
        # 单人游戏：新的客户端连接开始新的一局，结束之前的一局
        if self.game is not None:
            await self.game.stop()
        lock = asyncio.Lock()

        async def send(payload: dict | bytes) -> None:
            async with lock:
                try:
                    if isinstance(payload, bytes):
                        await ws.send_bytes(payload)
                    else:
                        await ws.send_text(json.dumps(payload, ensure_ascii=False))
                except Exception:
                    pass

        game = self.game = Game(self.scenario_path, send)
        await send({"type": "init", "player_id": game.player, "scenario": client_view(game.scenario)})
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await game.handle_audio(msg["bytes"])
                elif msg.get("text"):
                    await game.handle(json.loads(msg["text"]))
        except WebSocketDisconnect:
            pass
        finally:
            if self.game is game:
                await game.stop()
                self.game = None


def build_app(scenario_path: Path):
    server = Server(scenario_path)
    web = Starlette(routes=[
        WebSocketRoute("/ws", server.websocket),
        Mount("/", StaticFiles(directory=STATIC_DIR, html=True)),
    ])
    mcp_app = tools.mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # 平台从公网访问，Host 不固定，关闭 DNS rebinding 校验
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def router(scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "lifespan" or path.startswith("/mcp"):
            return await mcp_app(scope, receive, send)
        if scope["type"] == "http" and path.startswith("/llm/"):
            return await llm_gateway.app(dict(scope, path=path[len("/llm"):]), receive, send)
        return await web(scope, receive, send)

    return router


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if os.getenv("GAME_DEBUG") == "1":
        logging.getLogger("linx_game").setLevel(logging.DEBUG)
    scenario = Path(os.getenv("GAME_SCENARIO", REPO_ROOT / "scenarios" / "hotel_lobby" / "scenario.yaml"))
    uvicorn.run(build_app(scenario), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
