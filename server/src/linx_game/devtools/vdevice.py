"""灵矽虚拟设备。

按灵矽设备协议（OTA + WebSocket）接入平台，上行发送 PCM 音频，
收集平台返回的识别文本、表情、TTS 文本与音频，用于自动化端到端测试。

首次运行会通过 OTA 取得 6 位激活码，需在智控台将设备绑定到智能体；
设备标识保存在 VDEVICE_STATE（默认 ~/.config/linx-game/vdevice.json）。
"""

import argparse
import asyncio
import json
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import websockets
from dotenv import find_dotenv, load_dotenv

from linx_game.devtools import tts

OTA_URL = "https://xrobo.qiniuapi.com/v1/ota/"
STATE_PATH = Path(os.getenv("VDEVICE_STATE", Path.home() / ".config" / "linx-game" / "vdevice.json"))

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2


@dataclass
class Turn:
    """一轮对话中平台返回的内容。"""

    stt: str | None = None
    emotions: list[str] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    audio_bytes: int = 0
    first_reply_s: float | None = None
    other: list[dict] = field(default_factory=list)


def load_identity() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    # 本地管理地址（首字节 0x02），不与真实网卡冲突
    mac = "02:" + ":".join(f"{random.randint(0, 255):02X}" for _ in range(5))
    ident = {"mac": mac, "client_id": str(uuid.uuid4())}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(ident, indent=2))
    return ident


def ota(ident: dict) -> dict:
    headers = {
        "Activation-Version": "1",
        "Device-Id": ident["mac"],
        "Client-Id": ident["client_id"],
        "User-Agent": "linx-game-vdevice/0.1.0",
        "Accept-Language": "zh-CN",
    }
    body = {
        "version": 0,
        "uuid": ident["client_id"],
        "application": {"name": "linx-game-vdevice", "version": "0.1.0", "elf_sha256": "0" * 64},
        "board": {"type": "linx-game-vdevice", "ssid": "none", "rssi": 0, "mac": ident["mac"]},
        "mac_address": ident["mac"],
        "chip_model_name": "esp32s3",
        "flash_size": 16 * 1024 * 1024,
    }
    resp = httpx.post(OTA_URL, headers=headers, json=body, timeout=20)
    resp.raise_for_status()
    return resp.json()


class VirtualDevice:
    def __init__(self, ident: dict, ws_url: str, token: str):
        self.ident = ident
        self.ws_url = ws_url
        self.token = token
        self.ws = None
        self.session_id = None

    @classmethod
    def from_ota(cls) -> "VirtualDevice":
        ident = load_identity()
        info = ota(ident)
        if "activation" in info:
            code = info["activation"]["code"]
            raise SystemExit(f"设备 {ident['mac']} 未绑定，请在智控台用激活码 {code} 绑定后重试")
        ws = info["websocket"]
        return cls(ident, ws["url"], ws["token"])

    async def __aenter__(self):
        self.ws = await websockets.connect(
            self.ws_url,
            additional_headers={
                "Authorization": f"Bearer {self.token}",
                "Device-Id": self.ident["mac"],
                "Client-Id": self.ident["client_id"],
                "Protocol-Version": "1",
            },
            max_size=None,
        )
        await self.ws.send(json.dumps({
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "pcm",
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "bit_depth": 16,
                "endianness": "little",
                "frame_duration": FRAME_MS,
                "frame_size": FRAME_BYTES // 2,
                "sample_format": "signed_int16",
                "play_buffer_duration": 1000,
            },
        }))
        while True:
            msg = await asyncio.wait_for(self.ws.recv(), timeout=10)
            if isinstance(msg, str) and json.loads(msg).get("type") == "hello":
                self.hello = json.loads(msg)
                self.session_id = self.hello.get("session_id")
                return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def _send_json(self, payload: dict) -> None:
        if self.session_id:
            payload = {"session_id": self.session_id, **payload}
        await self.ws.send(json.dumps(payload))

    async def say(self, pcm: bytes, timeout: float = 30) -> Turn:
        """按实时节奏发送一段语音，并收集平台对这一轮的全部响应。"""
        await self._send_json({"type": "listen", "state": "start", "mode": "manual"})
        for i in range(0, len(pcm), FRAME_BYTES):
            await self.ws.send(pcm[i : i + FRAME_BYTES])
            await asyncio.sleep(FRAME_MS / 1000)
        # 尾部静音，便于 VAD 判断语句结束
        for _ in range(300 // FRAME_MS):
            await self.ws.send(b"\0" * FRAME_BYTES)
            await asyncio.sleep(FRAME_MS / 1000)
        await self._send_json({"type": "listen", "state": "stop"})
        sent_at = time.monotonic()

        turn = Turn()
        deadline = sent_at + timeout
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=deadline - time.monotonic())
            except TimeoutError:
                break
            if isinstance(msg, bytes):
                turn.audio_bytes += len(msg)
                continue
            data = json.loads(msg)
            kind, state = data.get("type"), data.get("state")
            if kind == "stt":
                turn.stt = data.get("text")
            elif kind == "llm":
                turn.emotions.append(data.get("emotion"))
            elif kind == "tts" and state == "sentence_start":
                if turn.first_reply_s is None:
                    turn.first_reply_s = round(time.monotonic() - sent_at, 2)
                turn.replies.append(data.get("text"))
            elif kind == "tts" and state == "stop":
                break
            elif kind != "tts":
                turn.other.append(data)
        return turn


async def _run(args) -> None:
    load_dotenv(find_dotenv(usecwd=True))
    async with VirtualDevice.from_ota() as dev:
        print(f"已连接 session={dev.session_id} 下行音频参数={dev.hello.get('audio_params')}")
        for text in args.text:
            pcm = tts.synthesize(text, voice=args.voice, style=args.style)
            turn = await dev.say(pcm)
            print(json.dumps({"voice": args.voice, "said": text, **turn.__dict__}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="灵矽虚拟设备：用合成语音与智能体对话")
    parser.add_argument("text", nargs="+", help="依次说出的句子，每句为一轮")
    parser.add_argument("--voice", default="冰糖", choices=tts.VOICES)
    parser.add_argument("--style", help="语气描述，如「愤怒地，语速很快」")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
