"""一局游戏的运行时：世界、AI 角色的灵矽连接、网页客户端之间的接线。"""

import asyncio
import json
from dataclasses import asdict
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from linx_game import llm_gateway
from linx_game.devtools.provision import STATE_PATH
from linx_game.engine.scenario import Scenario, load_scenario
from linx_game.engine.world import Event, World
from linx_game.runtime import tools
from linx_game.runtime.brain import Brain
from linx_game.runtime.judge import judge
from linx_game.runtime.session import LinxSession
from linx_game.runtime.voice import Ear, Mouth, VoiceRouter

log = logging.getLogger("linx_game.game")

# 平台连接建立后，MCP 工具异步加载，约 5 秒后才出现在模型的工具列表中
TOOLS_READY_S = 6
STATE_HZ = 5

Send = Callable[[dict | bytes], Awaitable[None]]


def client_view(scenario: Scenario) -> dict:
    """网页 init 消息中的剧本内容：只含地图与公开信息，不含角色背景、任务书与真相。"""
    return {
        "title": scenario.title, "intro": scenario.intro,
        "duration_s": scenario.duration_s, "start_clock": scenario.start_clock,
        "map": {
            "width": scenario.width, "height": scenario.height, "rows": list(scenario.rows),
            "legend": {ch: {k: v for k, v in asdict(cell).items() if v is not None}
                       for ch, cell in scenario.legend.items()},
        },
        "rooms": {rid: {"name": r.name, "offstage": r.offstage} for rid, r in scenario.rooms.items()},
        "interactables": [{"id": it.id, "name": it.name, "pos": list(it.pos),
                           "actions": [{"id": a.id, "label": a.label} for a in it.actions
                                       if a.who is None or any(c.player and c.id in a.who
                                                               for c in scenario.characters)]}
                          for it in scenario.interactables],
    }


class Game:
    def __init__(self, scenario_path: Path, send: Send):
        self.scenario = load_scenario(scenario_path)
        self.world: World | None = None
        self.player = next(c.id for c in self.scenario.characters if c.player)
        self.send = send
        # 设为 1 时不连接灵矽：AI 角色不行动，台词按字数模拟播放，用于调试界面
        self.offline = os.getenv("GAME_OFFLINE") == "1"
        self.sessions: list[LinxSession] = []
        self.brains: dict[str, Brain] = {}
        self.ear: Ear | None = None
        self.tasks: list[asyncio.Task] = []
        self.ended = False

    async def start(self) -> None:
        """玩家读完背景介绍后开局：创建世界、连接灵矽、开始计时。"""
        if self.world is not None:
            return
        self.world = World(self.scenario)
        if not self.offline:
            await self._connect()
        self.world.subscribe(self._on_event)
        self.tasks += [
            asyncio.create_task(self.world.run()),
            asyncio.create_task(self._push_state()),
            asyncio.create_task(self._timer()),
        ]
        if self.brains:
            self.tasks.append(asyncio.create_task(self._start_brains()))

    async def _connect(self) -> None:
        state = json.loads(STATE_PATH.read_text())
        router = VoiceRouter()
        for char in self.scenario.characters:
            key = f"{self.scenario.id}.{char.id}"
            agent_id = state["agents"][key]
            if char.player:
                session = LinxSession(f"{char.id}.ear", state["devices"][f"{key}.ear"], agent_id)
                self.ear = Ear(self.world, char.id, session)
                llm_gateway.silent_devices.add(session.mac)
                self.sessions.append(session)
                continue
            brain_session = LinxSession(f"{char.id}.brain", state["devices"][f"{key}.brain"], agent_id)
            mouth_session = LinxSession(f"{char.id}.mouth", state["devices"][f"{key}.mouth"], agent_id)
            brain = self.brains[char.id] = Brain(self.world, char.id, brain_session)
            router.mouths[char.id] = Mouth(self.world, char.id, mouth_session, self._on_audio)
            llm_gateway.controllers[brain_session.mac] = brain
            llm_gateway.silent_devices.add(mouth_session.mac)
            tools.actors[brain_session.mac] = (self.world, char.id)
            self.sessions += [brain_session, mouth_session]
        await asyncio.gather(*(s.connect() for s in self.sessions))
        self.world.voice = router

    async def _start_brains(self) -> None:
        await asyncio.sleep(TOOLS_READY_S)
        self.tasks += [asyncio.create_task(b.run()) for b in self.brains.values()]

    async def stop(self) -> None:
        self.ended = True
        if self.world is None:
            return
        for task in self.tasks:
            task.cancel()
        for session in self.sessions:
            llm_gateway.controllers.pop(session.mac, None)
            llm_gateway.silent_devices.discard(session.mac)
            tools.actors.pop(session.mac, None)
        await asyncio.gather(*(s.close() for s in self.sessions), return_exceptions=True)

    async def end(self, reason: str) -> None:
        if self.ended:
            return
        self.ended = True
        await self.send({"type": "ended", "reason": reason, "verdict": None})
        verdict = await judge(self.world, self.player)
        await self.send({"type": "ended", "reason": reason, "verdict": verdict})
        await self.stop()

    async def _timer(self) -> None:
        await asyncio.sleep(max(0.0, self.scenario.duration_s - self.world.now()))
        asyncio.create_task(self.end("timeout"))

    async def _push_state(self) -> None:
        while True:
            snapshot = self.world.snapshot()
            snapshot["remaining_s"] = max(0, round(self.scenario.duration_s - self.world.now()))
            await self.send({"type": "state", **snapshot})
            await asyncio.sleep(1 / STATE_HZ)

    def _on_event(self, event: Event) -> None:
        if event.kind == "speech" and event.data["cid"] == self.player:
            # 玩家自己说的话也显示在字幕中
            asyncio.create_task(self.send({"type": "speech", "cid": self.player, "name": "你",
                                           "sentence": event.data["sentence"], "level": "clear", "gain": 1.0}))
        if event.kind != "percept":
            return
        cid, percept = event.data["cid"], event.data["percept"]
        if cid == self.player:
            asyncio.create_task(self._send_percept(percept))
        elif percept.wake and cid in self.brains:
            self.brains[cid].wake()

    async def _send_percept(self, percept) -> None:
        await self.send({"type": "percept", "t": percept.t, "clock": self.world.clock_text(percept.t),
                         "kind": percept.kind, "text": percept.text, "data": percept.data})
        if percept.kind in ("speech", "muffled"):
            data = percept.data
            await self.send({"type": "speech", "cid": data.get("speaker"), "name": data.get("name", ""),
                             "sentence": data.get("sentence", "") if percept.kind == "speech" else "",
                             "level": "clear" if percept.kind == "speech" else "muffled",
                             "gain": data.get("gain", 1.0)})

    def _on_audio(self, cid: str, pcm: bytes) -> None:
        level, gain = self.world.audibility(cid, self.player)
        if level is None:
            return
        header = json.dumps({"cid": cid, "gain": round(gain, 3)}).encode()
        asyncio.create_task(self.send(len(header).to_bytes(2, "little") + header + pcm))

    # 来自网页客户端的操作
    async def handle(self, msg: dict) -> None:
        kind, world, me = msg.get("type"), self.world, self.player
        if kind == "start":
            await self.start()
            return
        if world is None or self.ended:
            return
        if kind == "step":
            dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}.get(msg.get("dir"), (0, 0))
            world.step(me, dx, dy)
        elif kind == "move_to":
            world.move_to(me, tuple(msg["pos"]))
        elif kind == "interact":
            result = world.interact(me, msg["target"], msg["action"])
            await self.send({"type": "notice", "text": result})
        elif kind == "stop":
            world.stop(me)
        elif kind == "photo":
            photo = world.photo(me)
            await self.send({"type": "photo", "index": len(world.photos.get(me, [])), "scene": photo})
        elif kind == "say_text" and msg.get("text"):
            world.player_said(me, msg["text"])
        elif kind == "submit":
            asyncio.create_task(self.end("submitted"))
        elif kind == "mic" and self.ear is not None:
            await (self.ear.start() if msg.get("state") == "start" else self.ear.stop())

    async def handle_audio(self, pcm: bytes) -> None:
        if self.ear is not None:
            await self.ear.feed(pcm)
