"""角色发声与玩家语音识别。

Mouth：每个 AI 角色一条连接，只用 text_response 播报台词（不经过模型）。平台逐句下发
sentence_start 与 PCM 音频，据此向世界报告已说出的句子，并把音频交给网页层按距离播放。
Ear：玩家的连接，转发麦克风音频，取平台的识别文本作为玩家说出的话。
"""

import asyncio
import logging
from collections.abc import Callable

from linx_game.engine.world import World
from linx_game.runtime.session import LinxSession

log = logging.getLogger("linx_game.voice")


class Mouth:
    """一个角色的发声连接。同一时间只播一段台词：新台词提交时平台会自动中止正在播放的那段。"""

    def __init__(self, world: World, cid: str, session: LinxSession, on_audio: Callable[[str, bytes], None]):
        self.world = world
        self.cid = cid
        self.session = session
        self.current = None
        session.on_json = self._on_json
        session.on_audio = self._on_audio
        self._forward = on_audio

    def start(self, speech) -> None:
        self.current = speech
        asyncio.create_task(self.session.send_text("（播报台词）", text_response=speech.text))

    def abort(self, speech) -> None:
        # 引擎已自行结束被中止的台词，这里只需让平台停止播放
        if speech is self.current:
            asyncio.create_task(self.session.abort())

    def _live(self):
        speech = self.current
        return speech if speech is not None and not speech.done else None

    def _on_audio(self, pcm: bytes) -> None:
        if self._live() is not None:
            self._forward(self.cid, pcm)

    def _on_json(self, data: dict) -> None:
        if data.get("type") != "tts" or (speech := self._live()) is None:
            return
        state, text = data.get("state"), data.get("text") or ""
        # 被中止的旧台词的消息可能晚到，按句子内容核对归属
        if state == "sentence_start" and text and text.strip() in speech.text:
            self.world.speech_sentence(self.cid, speech.id, text)
        elif state == "stop" and not data.get("is_aborted"):
            self.world.speech_finished(self.cid, speech.id, aborted=False)


class VoiceRouter:
    """引擎的语音后端：按说话者把台词分派给对应角色的 Mouth。"""

    def __init__(self):
        self.mouths: dict[str, Mouth] = {}

    def start(self, speech) -> None:
        self.mouths[speech.cid].start(speech)

    def abort(self, speech) -> None:
        self.mouths[speech.cid].abort(speech)


class Ear:
    def __init__(self, world: World, cid: str, session: LinxSession):
        self.world = world
        self.cid = cid
        self.session = session
        session.on_json = self._on_json

    async def start(self) -> None:
        await self.session.send_json({"type": "listen", "state": "start", "mode": "manual"})

    async def feed(self, pcm: bytes) -> None:
        await self.session.send_audio(pcm)

    async def stop(self) -> None:
        await self.session.send_json({"type": "listen", "state": "stop"})

    def _on_json(self, data: dict) -> None:
        if data.get("type") == "stt" and data.get("text"):
            log.info("player said: %s", data["text"])
            self.world.player_said(self.cid, data["text"])
