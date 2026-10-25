"""AI 角色的思考循环。

每个角色一条 brain 连接：有值得反应的感知（或长时间无事）时，把感知文本作为一轮输入发给灵矽，
平台调用大模型（经本服务的网关），模型以工具调用行动。网关按本模块组织的上下文请求模型，
并在本轮结束时通知这里，再发送下一轮，保证同一角色同一时间只有一轮在进行。

角色的记忆由本模块保存：每轮的感知、内心想法、工具调用及其结果，不依赖平台的对话历史。
"""

import asyncio
import logging

from linx_game.engine import perception
from linx_game.engine.world import World
from linx_game.runtime.session import LinxSession

log = logging.getLogger("linx_game.brain")

# 剧情进展由感知与提示驱动；没有新感知时，最长隔多久再给角色一次思考机会（秒）
IDLE_S = 60
# 被唤醒后等待感知平静下来再思考：一段话按句到达，等对方说完再一并处理（秒）
SETTLE_S = 1.5
SETTLE_MAX_S = 6
# 等待一轮结束的上限（秒），超时后继续下一轮
TURN_TIMEOUT_S = 40
# 保留的历史消息条数（不含系统提示词与示例）
HISTORY_LIMIT = 40

PROMPT = """你在操控一场实时语音推理游戏中的角色「{name}」（{role}）。

## 如何行动
- 你只能通过工具影响世界：说话用 say，移动用 move_to，操作物品用 interact，放弃正在做的事用 stop，中止正在说的话用 stop_speaking，继续手上的事或等待用 wait（想过一会儿再行动时可指定秒数）。
- 正文不会被任何人看到或听到。正文只写角色此刻的内心想法，一两句即可。说出口的每一句话都必须通过 say。
- 世界按真实时间流动，不会等你。你每次收到的是角色此刻的感知。一次可以调用零到多个工具；暂时不需要行动时，只写想法、不调用工具。
- 说话像真人对话：口语、简短，一次一两句。只有附近的人听得到你，离得远的人听不到或听不清。
- 你只知道角色背景和感知中出现的信息。只有【新的感知】里出现的事才真的发生了，不要臆想没有感知到的事（例如没有来电就不要当作接到了电话）。
- 身边没有人可以交谈时不要说话，不要自言自语。
- 任务书里的安排有先后条件，前提还没有发生（例如还没有人告诉你某件事），就不要提前去做。
- 没有新情况时保持现状，不要为了行动而行动；等待时不要来回走动，也不要重复说过的话。
- move_to 的目标只能是下面列出的地点、感知中出现的人或可交互的东西；进入某个房间就 move_to 这个房间的名字。
- interact 的行动只能从感知里列出的「可以……（行动 xxx）」中选。

## 地图上的地点
{places}

## 角色背景
{card}

## 任务书
{task}
"""

# 一组通用示例，演示正文写想法、行动走工具
EXAMPLE = [
    {"role": "user", "content": "【14:02:10】你在大堂休息区。\n你看到：一位穿灰色外套的客人在沙发上看手机。\n新的感知：\n- 14:02:08 客人（大堂休息区，正常）：「请问这附近有便利店吗？」"},
    {"role": "assistant", "content": "有人问路，顺手答一句，别耽误我的事。", "tool_calls": [
        {"id": "example_0", "type": "function",
         "function": {"name": "say", "arguments": "{\"text\": \"出门右转走两分钟就有一家。\", \"volume\": \"正常\"}"}}]},
    {"role": "tool", "tool_call_id": "example_0", "content": "开始说话。"},
    {"role": "assistant", "content": ""},
]


class Brain:
    def __init__(self, world: World, cid: str, session: LinxSession):
        self.world = world
        self.cid = cid
        self.session = session
        session.on_json = self._on_platform
        self.thoughts: list[tuple[float, str]] = []
        self.history: list[dict] = []
        self._pending_calls: list[dict] = []
        self._results: list[str] = []
        self._wake = asyncio.Event()
        self._turn_done = asyncio.Event()
        char = next(c for c in world.scenario.characters if c.id == cid)
        places = "、".join(room.name for room in world.scenario.rooms.values())
        self.system = PROMPT.format(name=char.name, role=char.role, places=places,
                                    card=char.card.strip(), task=char.task.strip())

    # 网关回调（llm_gateway.Controller）
    def build(self, messages: list[dict]) -> list[dict]:
        history = self.history[-HISTORY_LIMIT:]
        # 从一条感知开始，避免以孤立的工具结果开头
        while history and history[0].get("role") != "user":
            history = history[1:]
        return [{"role": "system", "content": self.system}, *EXAMPLE, *history]

    def on_reply(self, thought: str, tool_calls: list[dict]) -> None:
        if thought:
            self.thoughts.append((self.world.now(), thought))
            log.info("thought %s: %s", self.cid, thought)
        message = {"role": "assistant", "content": thought}
        for i, call in enumerate(tool_calls):
            call["id"] = call["id"] or f"call_{len(self.history)}_{i}"
        if tool_calls:
            message["tool_calls"] = tool_calls
        self.history.append(message)
        self._pending_calls = tool_calls

    def record_result(self, result: str) -> None:
        """行动工具的执行结果，按调用顺序到达。"""
        self._results.append(result)

    def _flush_results(self) -> None:
        for call in self._pending_calls:
            result = self._results.pop(0) if self._results else "已执行。"
            self.history.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        self._pending_calls = []
        self._results.clear()

    def on_turn_done(self) -> None:
        self._turn_done.set()

    def _on_platform(self, data: dict) -> None:
        log.debug("platform %s %s", self.cid, data)

    def wake_after(self, seconds: float) -> None:
        """角色要求过一段时间再想一想（wait 工具）。"""
        asyncio.get_running_loop().call_later(seconds, self.wake)

    def wake(self) -> None:
        self._wake.set()

    async def _settle(self) -> None:
        """唤醒后直到 SETTLE_S 内没有新的唤醒（最长 SETTLE_MAX_S）再继续。"""
        deadline = asyncio.get_running_loop().time() + SETTLE_MAX_S
        while True:
            self._wake.clear()
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                return
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(SETTLE_S, left))
            except TimeoutError:
                return

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=IDLE_S)
            except TimeoutError:
                pass
            await self._settle()
            text = perception.render(self.world, self.cid, self.world.drain(self.cid))
            self._turn_done.clear()
            self._flush_results()
            self.history.append({"role": "user", "content": text})
            log.info("perceive %s:\n%s", self.cid, text)
            await self.session.send_text(text)
            try:
                await asyncio.wait_for(self._turn_done.wait(), timeout=TURN_TIMEOUT_S)
            except TimeoutError:
                log.warning("turn timeout %s", self.cid)
