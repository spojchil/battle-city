"""与灵矽平台的长连接。

每条连接对应一台虚拟设备，后台持续接收平台消息并交给回调处理。
游戏中的角色各有两条连接：brain 接收感知并触发模型与工具调用，
mouth 只用 text_response 播报台词；玩家另有一条 ear 连接用于语音识别。
"""

import asyncio
import json
import logging
from collections.abc import Callable

import websockets

from linx_game.devtools.vdevice import VirtualDevice

log = logging.getLogger("linx_game.session")


class LinxSession:
    def __init__(self, name: str, ident: dict, agent_id: str):
        self.name = name
        self.ident = ident
        self.agent_id = agent_id
        self.on_json: Callable[[dict], None] = lambda data: None
        self.on_audio: Callable[[bytes], None] = lambda pcm: None
        self._device: VirtualDevice | None = None
        self._reader: asyncio.Task | None = None

    @property
    def mac(self) -> str:
        return self.ident["mac"]

    async def connect(self) -> None:
        # OTA 是同步请求，放到线程中执行，避免多条连接同时建立时阻塞事件循环
        device = await asyncio.to_thread(VirtualDevice.from_ota, self.ident, agent_id=self.agent_id)
        self._device = await device.__aenter__()
        self._reader = asyncio.create_task(self._read(), name=f"linx-{self.name}")
        log.info("connected %s mac=%s session=%s", self.name, self.mac, self._device.session_id)

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._device:
            await self._device.ws.close()

    async def _read(self) -> None:
        try:
            async for msg in self._device.ws:
                if isinstance(msg, bytes):
                    self.on_audio(msg)
                else:
                    self.on_json(json.loads(msg))
        except websockets.ConnectionClosed as exc:
            log.warning("connection closed %s: %s", self.name, exc)
        except Exception:
            log.exception("reader failed %s", self.name)

    async def send_json(self, payload: dict) -> None:
        await self._device._send_json(payload)

    async def send_text(self, text: str, text_response: str | None = None) -> None:
        payload = {"type": "listen", "state": "detect", "text": text}
        if text_response is not None:
            payload["text_response"] = text_response
        await self.send_json(payload)

    async def abort(self) -> None:
        await self.send_json({"type": "abort", "reason": "wake_word_detected"})

    async def send_audio(self, pcm: bytes) -> None:
        await self._device.ws.send(pcm)
