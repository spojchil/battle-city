"""为测试音色批量注册灵矽说话人与声纹。

每个音色单独建立一次会话说一段样本语音，再从该会话的聊天记录中取用户录音
创建声纹，最后追加绑定到智能体（保留已有绑定）。
"""

import argparse
import asyncio
import os
import time

import httpx
from dotenv import find_dotenv, load_dotenv

from linx_game.devtools import tts
from linx_game.devtools.vdevice import VirtualDevice

SAMPLE_TEXT = "大家好，我来做个自我介绍。我平时喜欢看书、爬山，也喜欢和朋友一起玩桌游，很高兴认识大家。"

# 测试音色与对应的说话人称呼
SPEAKERS = {"冰糖": "小冰", "茉莉": "小茉", "苏打": "阿苏", "白桦": "老白"}

CHAT_TYPE_USER = 1


class LinxApi:
    def __init__(self):
        self.base = os.environ["LINX_BASE_URL"]
        self.headers = {"Authorization": f"Bearer {os.environ['LINX_API_KEY']}"}

    def call(self, method: str, path: str, **kwargs) -> dict:
        resp = httpx.request(method, self.base + path, headers=self.headers, timeout=30, **kwargs)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"{method} {path} 失败：{body}")
        return body.get("data") or {}


def user_audio_url(api: LinxApi, agent_id: str, session_id: str, retries: int = 10) -> str:
    for _ in range(retries):
        records = api.call("GET", f"/v1/agents/{agent_id}/chat-history").get("records", [])
        for r in records:
            if r.get("session_id") == session_id and r.get("chat_type") == CHAT_TYPE_USER and r.get("audio_url"):
                return r["audio_url"]
        time.sleep(2)
    raise RuntimeError(f"会话 {session_id} 中未找到用户录音")


async def record_sample(voice: str) -> str:
    async with VirtualDevice.from_ota() as dev:
        await dev.say(tts.synthesize(SAMPLE_TEXT, voice=voice))
        return dev.session_id


async def _run(voices: list[str]) -> None:
    load_dotenv(find_dotenv(usecwd=True))
    api = LinxApi()
    agent_id = os.environ["LINX_AGENT_ID"]
    existing = {s["name"]: s["speaker_id"] for s in api.call("GET", "/v1/speakers").get("speakers", [])}

    for voice in voices:
        name = SPEAKERS[voice]
        session_id = await record_sample(voice)
        audio_url = user_audio_url(api, agent_id, session_id)
        speaker_id = existing.get(name) or api.call("POST", "/v1/speakers", json={"name": name, "desc": f"测试音色：{voice}"})["speaker_id"]
        api.call("POST", f"/v1/speakers/{speaker_id}/voiceprint", json={"audio_url": audio_url})
        existing[name] = speaker_id
        print(f"{voice} → {name}（{speaker_id}）已录入声纹")

    bound = api.call("GET", f"/v1/agents/{agent_id}/speaker-config").get("speaker_ids") or []
    wanted = list(dict.fromkeys(bound + [existing[SPEAKERS[v]] for v in voices]))
    api.call("PUT", f"/v1/agents/{agent_id}/speaker-config", json={"speaker_ids": wanted})
    print(f"智能体已绑定说话人：{len(wanted)} 个")


def main() -> None:
    parser = argparse.ArgumentParser(description="为测试音色注册灵矽说话人与声纹")
    parser.add_argument("voices", nargs="*", choices=list(SPEAKERS), help="默认全部音色")
    asyncio.run(_run(parser.parse_args().voices or list(SPEAKERS)))


if __name__ == "__main__":
    main()
