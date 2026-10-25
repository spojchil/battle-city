"""世界状态：角色的移动、说话与交互，以及感知的生成与分发。

世界按真实时间 1:1 推进，所有角色（含玩家）遵循同一套规则。`tick(dt)` 推进移动、交互与
模拟语音；意图方法立即返回一句中文结果，用户错误不抛异常。感知在事件发生的那一刻按当时
位置生成，写入各角色收件箱，由 `drain` 取走；同时以 Event 推送给订阅者（网页层）。
"""

import asyncio
import itertools
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .grid import DIRS, Grid, chebyshev, direction
from .perception import THROUGH_DOOR_NOTE, Percept, join_describe
from .scenario import Action, CharacterSpec, Interactable, Pos, Scenario
from .sound import CLEAR, VOLUMES
from .sound import audibility as sound_level

log = logging.getLogger("linx_game.engine")

STEP_S = 1 / 1.5            # 步行速度约 1.5 格/秒
SIM_CHAR_S = 0.22           # 无语音后端时，模拟播放每字耗时
FOOTSTEP_COST = 6           # 脚步声可闻的声音代价上限
FOOTSTEP_REPEAT_S = 10      # 同一对角色的脚步声感知间隔
RECORDED_KINDS = ("speech", "muffled", "sound")   # 录音笔记录的感知种类
VIS_SETTLE_S = 1.0          # 进出视野的确认延迟：此时间内反复进出（如穿过一格宽的区域）相互抵消

_SPEAK_VERB = {"耳语": "低声说", "正常": "说", "大喊": "大声喊"}
_MUFFLED_VERB = {"耳语": "低声说话", "正常": "说话", "大喊": "大声喊话"}
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+(?:[。！？!?；;]+[”’\"」』）)]*)?")


def split_sentences(text: str) -> list[str]:
    """按句末标点与换行切句，标点（及其后的右引号、右括号）留在句尾。"""
    return [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]


def flags_match(cond: dict, flags: dict) -> bool:
    return all(flags.get(k) == v for k, v in cond.items())


@dataclass
class Speech:
    id: int
    cid: str
    text: str
    volume: str
    sentences: list[str]
    t: float
    spoken: list[str] = field(default_factory=list)   # 已开始播放的句子
    done: bool = False
    aborted: bool = False
    _next: int = field(default=0, repr=False)          # 模拟播放：下一句序号
    _left: float = field(default=0.0, repr=False)      # 模拟播放：当前句剩余秒数

    @property
    def spoken_text(self) -> str:
        return "".join(self.spoken)


class VoiceBackend(Protocol):
    """语音后端：合成并播放角色的话。

    每句开始播放时回调 `world.speech_sentence`，全部播完或中止后回调 `world.speech_finished`。
    引擎中止一段话时会先自行记为结束再调用 `abort`，此后关于该段话的回调会被忽略。
    """

    def start(self, speech: Speech) -> None: ...

    def abort(self, speech: Speech) -> None: ...


@dataclass
class Event:
    kind: str     # moved / speech / speech_end / job_start / job_end / sound / flag / percept
    t: float
    data: dict


@dataclass
class Job:
    interactable: Interactable
    action: Action
    progress: float              # 已进行的秒数（cumulative 行动含之前保留的进度）
    sound_acc: float = 0.0

    @property
    def key(self) -> tuple[str, str]:
        return self.interactable.id, self.action.id


@dataclass
class Character:
    spec: CharacterSpec
    pos: Pos
    path: list[Pos] = field(default_factory=list)   # 剩余的寻路步
    dest: str | None = None                         # 寻路目的地的描述
    pending: tuple[Interactable, Action] | None = None   # 到达后自动开始的交互
    move_acc: float = 0.0
    last_step_t: float = -math.inf
    job: Job | None = None
    speech: Speech | None = None
    inbox: list[Percept] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def role(self) -> str:
        return self.spec.role

    @property
    def player(self) -> bool:
        return self.spec.player


class World:
    def __init__(self, scenario: Scenario, clock: Callable[[], float] = time.monotonic):
        self.scenario = scenario
        self.grid = Grid(scenario)
        self._clock = clock
        self._t0 = clock()
        self.characters = {s.id: Character(s, s.start) for s in scenario.characters}
        self.flags: dict = {}
        for it in scenario.interactables:
            self.flags.update(it.flags)
        self._pending_cues = list(scenario.cues)
        self.progress: dict[tuple[str, str], float] = {}   # cumulative 行动被打断后保留的进度
        self.recordings: dict[str, list[Percept]] = {c.id: [] for c in scenario.characters if c.player}
        self.photos: dict[str, list[dict]] = {}
        self.voice: VoiceBackend | None = None
        self._subscribers: list[Callable[[Event], None]] = []
        self._speech_ids = itertools.count(1)
        self._footsteps: dict[tuple[str, str], float] = {}   # (听者, 走动者) -> 上次感知时刻
        self._vis_pending: dict[tuple[str, str], tuple[bool, float]] = {}   # (观察者, 走动者) -> (原先是否可见, 变化时刻)

    # ---- 时间 ----

    def now(self) -> float:
        """开局以来的秒数。"""
        return self._clock() - self._t0

    def clock_text(self, t: float | None = None) -> str:
        """游戏内钟点，例如 14:03:20。"""
        t = self.now() if t is None else t
        s = (self.scenario.start_clock_s + int(t + 1e-6)) % 86400   # 容忍浮点累加误差
        return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"

    def tick(self, dt: float) -> None:
        """推进移动、交互与模拟语音。"""
        for c in list(self.characters.values()):
            if c.path:
                c.move_acc += dt
                while c.path and c.move_acc >= STEP_S:
                    c.move_acc -= STEP_S
                    self._move(c, c.path.pop(0))
                if not c.path:
                    self._arrive(c)
            if c.job:
                self._advance_job(c, dt)
            if self.voice is None and c.speech:
                self._advance_sim(c, dt)
        self._fire_cues()
        self._flush_visibility()

    def _fire_cues(self) -> None:
        now = self.now()
        for cue in list(self._pending_cues):
            if (cue.at_s is None or now >= cue.at_s) and flags_match(cue.when, self.flags):
                self._pending_cues.remove(cue)
                self._perceive(self.characters[cue.to], "cue", cue.text, True)

    async def run(self, hz: float = 10) -> None:
        """以固定频率循环调用 tick，直到被取消。"""
        last = self.now()
        while True:
            await asyncio.sleep(1 / hz)
            t = self.now()
            self.tick(t - last)
            last = t

    # ---- 移动 ----

    def move_to(self, cid: str, target: str | Pos) -> str:
        """前往房间、角色或可交互点（按名称或 id），也可直接给出格子坐标 (x, y)。"""
        c = self.characters[cid]
        if isinstance(target, str):
            found = self._resolve_place(c, target)
            if found is None:
                return f"找不到名为「{target}」的地点"
            label, is_goal = found
        else:
            label, is_goal = self._resolve_pos(c, (int(target[0]), int(target[1])))
        path = self.grid.find_path(c.pos, is_goal, lambda p: self._walkable(c, p))
        if path is None:
            return f"无法到达{label}"
        if not path:
            c.path, c.dest, c.pending = [], None, None
            return f"你已经在{label}"
        prefix = self._walk(c, path, label)
        return prefix + f"你开始走向{label}，约 {len(path) * STEP_S:.0f} 秒到达"

    def step(self, cid: str, dx: int, dy: int) -> bool:
        """单格移动（玩家键盘操作），受步行速度限制；成功时取消寻路与交互。"""
        c = self.characters[cid]
        if abs(dx) + abs(dy) != 1 or self.now() - c.last_step_t < STEP_S:
            return False
        nxt = (c.pos[0] + dx, c.pos[1] + dy)
        if not self._walkable(c, nxt):
            return False
        self._interrupt(c)
        self._move(c, nxt)
        return True

    def stop(self, cid: str) -> str:
        """停止移动与交互。"""
        return self._interrupt(self.characters[cid]) or "你没有在走动或做事"

    # ---- 交互 ----

    def interact(self, cid: str, target: str, action: str) -> str:
        """执行可交互点的行动；target 与 action 可用名称或 id。

        不在 1 格内时先走到可交互点旁最近的可站立格，到达后自动开始；途中的移动、step、stop 会取消。
        """
        c = self.characters[cid]
        it = _match(target, [(i.id, i.name, i) for i in self.scenario.interactables])
        if it is None:
            return f"找不到名为「{target}」的可交互物"
        act = _match(action, [(a.id, a.label, a) for a in it.actions])
        if act is None:
            options = "、".join(a.label for a in it.actions if self.can_do(cid, a))
            return (f"{it.name}没有「{action}」这个行动" + (f"，现在可以：{options}" if options else "")
                    + "。要去某个地方或进入某个房间，请用 move_to")
        if act.who is not None and cid not in act.who:
            return f"你不能{act.label}"
        if not flags_match(act.requires, self.flags):
            return f"现在无法{act.label}"
        if chebyshev(c.pos, it.pos) > 1:
            path = self.grid.find_path(c.pos, lambda p: chebyshev(p, it.pos) <= 1, lambda p: self._walkable(c, p))
            if path is None:
                return f"无法走到{it.name}旁边"
            prefix = self._walk(c, path, f"{it.name}旁边", pending=(it, act))
            return prefix + f"你走向{it.name}，到达后开始{act.label}"
        if c.job and c.job.key == (it.id, act.id):
            return f"你已经在{act.label}了"

        self._interrupt(c)
        key = (it.id, act.id)
        progress = self.progress.pop(key, 0.0) if act.cumulative else 0.0
        c.job = Job(it, act, progress)
        self._emit("job_start", cid=cid, interactable=it.id, action=act.id,
                   duration_s=act.duration_s, progress=progress)
        if act.sound:
            self._emit_sound(c, act)
        if self._advance_job(c, 0):
            return f"你完成了{act.label}"
        resumed = "（接着之前的进度）" if progress else ""
        return f"你开始{act.label}{resumed}，还需约 {act.duration_s - progress:.0f} 秒"

    def audibility(self, src_cid: str, dst_cid: str, volume: str | None = None) -> tuple[str | None, float]:
        """按两人当前位置计算 src 的声音传到 dst 的 (clear|muffled|None, 增益)。

        volume 省略时取 src 正在说的话的音量，没有在说话时按「正常」。
        """
        src, dst = self.characters[src_cid], self.characters[dst_cid]
        if volume is None:
            volume = src.speech.volume if src.speech else "正常"
        level, gain = sound_level(self.grid.sound_cost(src.pos, dst.pos), volume)
        return level, round(gain, 3)

    def can_do(self, cid: str, action: Action) -> bool:
        """角色身份与当前标志是否允许执行该行动（不考虑距离）。"""
        return (action.who is None or cid in action.who) and flags_match(action.requires, self.flags)

    def describe(self, it: Interactable) -> list[str]:
        """可交互点在当前标志下成立的状态描述。"""
        return [d.text for d in it.describe if flags_match(d.when, self.flags)]

    # ---- 说话 ----

    def say(self, cid: str, text: str, volume: str = "正常") -> Speech:
        """开始说一段话，逐句播放并产生听觉感知；新的话会中止正在说的话。未知音量按「正常」处理。"""
        c = self.characters[cid]
        if volume not in VOLUMES:
            volume = "正常"
        if c.speech:
            self._abort_speech(c)
        sp = Speech(next(self._speech_ids), cid, text, volume, split_sentences(text), self.now())
        if not sp.sentences:
            sp.done = True
            return sp
        c.speech = sp
        if self.voice is None:
            self._advance_sim(c, 0)
        else:
            self.voice.start(sp)
        return sp

    def stop_speaking(self, cid: str) -> str:
        c = self.characters[cid]
        if not c.speech:
            return "你没有在说话"
        sp = self._abort_speech(c)
        return f"你停了下来，已说出「{sp.spoken_text}」" if sp.spoken else "你停了下来，一句也还没说出口"

    def speech_sentence(self, cid: str, speech_id: int, sentence: str) -> None:
        """语音后端报告某句开始播放：此刻按位置向听者生成感知。"""
        c = self.characters[cid]
        sp = c.speech
        if sp is None or sp.id != speech_id:
            return
        sp.spoken.append(sentence)
        self._hear_sentence(c, sentence, sp.volume, speech_id)

    def speech_finished(self, cid: str, speech_id: int, aborted: bool) -> None:
        c = self.characters[cid]
        sp = c.speech
        if sp is None or sp.id != speech_id:
            return
        sp.done, sp.aborted = True, aborted
        c.speech = None
        self._emit("speech_end", cid=cid, speech_id=speech_id, aborted=aborted, spoken=sp.spoken_text)

    def player_said(self, cid: str, text: str, volume: str = "正常") -> None:
        """玩家真人语音（已说出）：立即按句产生听觉感知。"""
        c = self.characters[cid]
        for sentence in split_sentences(text):
            self._hear_sentence(c, sentence, volume, None)

    # ---- 拍照与感知 ----

    def photo(self, cid: str) -> dict:
        """记录此刻感知到的场景，存入 photos[cid]。"""
        c = self.characters[cid]
        people = [{"id": o.id, "name": o.name, "role": o.role, "activity": self.activity(o),
                   "speaking": o.speech is not None, "through_door": self.through_door(cid, o.pos)}
                  for o in self.visible_characters(cid)]
        objects = [{"id": it.id, "name": it.name, "describe": self.describe(it),
                    "through_door": self.through_door(cid, it.pos)}
                   for it in self.visible_interactables(cid)]
        place = self.place_name(c.pos)
        lines = [f"{self.clock_text()} 摄于{place}"]
        for p in people:
            doing = "，".join(filter(None, [p["activity"], "正在说话" if p["speaking"] else ""]))
            lines.append(f"{p['name']}（{p['role']}）" + (f"：{doing}" if doing else "")
                         + (THROUGH_DOOR_NOTE if p["through_door"] else ""))
        for o in objects:
            if o["describe"]:
                lines.append(f"{o['name']}：{join_describe(o['describe'])}"
                             + (THROUGH_DOOR_NOTE if o["through_door"] else ""))
        photos = self.photos.setdefault(cid, [])
        scene = {"index": len(photos), "t": round(self.now(), 2), "clock": self.clock_text(), "place": place,
                 "pos": list(c.pos), "people": people, "objects": objects, "text": "\n".join(lines)}
        photos.append(scene)
        return scene

    def drain(self, cid: str) -> list[Percept]:
        """取走该角色累积的感知。"""
        c = self.characters[cid]
        out, c.inbox = c.inbox, []
        return out

    def snapshot(self) -> dict:
        """网页 state 消息的内容。"""
        t = self.now()
        return {
            "type": "state", "t": round(t, 2), "clock": self.clock_text(t),
            "remaining_s": round(max(0.0, self.scenario.duration_s - t), 2),
            "characters": [{
                "id": c.id, "name": c.name, "role": c.role, "pos": list(c.pos),
                "room": self.grid.room_at(c.pos), "player": c.player, "activity": self.activity(c),
                "speaking": c.speech is not None, "offstage": self.grid.is_offstage(c.pos),
            } for c in self.characters.values()],
            "interactables": [{"id": it.id, "describe": self.describe(it)} for it in self.scenario.interactables],
            "flags": dict(self.flags),
        }

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        self._subscribers.append(callback)

    # ---- 场景查询（供感知渲染使用）----

    def place_name(self, pos: Pos) -> str:
        room = self.grid.room_at(pos)
        if room:
            return self.scenario.rooms[room].name
        if self.grid.is_door(pos):
            names = [self.scenario.rooms[r].name for r in self.grid.door_rooms(pos)]
            return f"{'与'.join(names)}之间的门口" if len(names) > 1 else f"{''.join(names)}门口"
        return f"({pos[0]}, {pos[1]})"

    def visible_characters(self, cid: str) -> list[Character]:
        """该角色能看见的其他角色：同一房间（站在门口时为相邻房间），以及透过虚掩的门看到的房间。"""
        c = self.characters[cid]
        return [o for o in self.characters.values() if o is not c and self._sees(c.pos, o.pos)]

    def visible_interactables(self, cid: str) -> list[Interactable]:
        rooms = self._view(self.characters[cid].pos)
        return [it for it in self.scenario.interactables if it.room in rooms]

    def through_door(self, cid: str, pos: Pos) -> bool:
        """该角色是否只是透过虚掩的门才看见 pos 处的人或物。"""
        return not (self._seen_rooms(self.characters[cid].pos) & self._seen_rooms(pos))

    def activity(self, c: Character) -> str:
        """别人看到的该角色在做什么。"""
        if c.job:
            return c.job.action.seen_as
        return "正在走动" if c.path else ""

    # ---- 内部 ----

    def _emit(self, kind: str, **data) -> None:
        event = Event(kind, self.now(), data)
        for cb in self._subscribers:
            try:
                cb(event)
            except Exception:
                log.exception("event subscriber failed: %s", kind)

    def _perceive(self, c: Character, kind: str, text: str, wake: bool, **data) -> Percept:
        p = Percept(self.now(), kind, text, wake, data)
        c.inbox.append(p)
        if c.id in self.recordings and kind in RECORDED_KINDS:
            self.recordings[c.id].append(p)
        self._emit("percept", cid=c.id, percept=p)
        return p

    def _walkable(self, c: Character, pos: Pos) -> bool:
        return self.grid.walkable(pos, c.id, c.player)

    def _seen_rooms(self, pos: Pos) -> set[str]:
        room = self.grid.room_at(pos)
        if room:
            return {room}
        return set(self.grid.door_rooms(pos)) if self.grid.is_door(pos) else set()

    def _view(self, pos: Pos) -> set[str]:
        """站在 pos 能看见的房间：所在房间，加上相邻虚掩门另一侧的房间。"""
        rooms = self._seen_rooms(pos)
        for dx, dy in DIRS:
            door = (pos[0] + dx, pos[1] + dy)
            cell = self.grid.cell(door)
            if cell and cell.kind == "door" and cell.see_through_when is not None \
                    and flags_match(cell.see_through_when, self.flags):
                rooms = rooms | set(self.grid.door_rooms(door))
        return rooms

    def _sees(self, observer: Pos, target: Pos) -> bool:
        """站在 observer 的角色能否看见 target 处的角色。虚掩的门只让门边的人看见另一侧，不对称。"""
        return bool(self._view(observer) & self._seen_rooms(target))

    def _resolve_place(self, c: Character, target: str) -> tuple[str, Callable[[Pos], bool]] | None:
        """解析目的地，返回 (描述, 目标格判定)。

        先在房间、角色、可交互点中找 id 或名称完全一致的，再按同样顺序做包含匹配，
        避免「水井暗门」这类名称被房间「水井」抢先匹配。
        """
        rooms = self.scenario.rooms
        others = [o for o in self.characters.values()
                  if o is not c and (not self.grid.is_offstage(o.pos) or self._sees(c.pos, o.pos))]
        candidates = [
            [(r.id, r.name, ("room", r)) for r in rooms.values()],
            [(o.id, o.name, ("character", o)) for o in others],
            [(i.id, i.name, ("interactable", i)) for i in self.scenario.interactables],
        ]
        found = (next((_exact(target, items) for items in candidates if _exact(target, items)), None)
                 or next((_match(target, items) for items in candidates if _match(target, items)), None))
        if found is None:
            return None
        kind, obj = found
        if kind == "room":
            return obj.name, lambda p: self.grid.room_at(p) == obj.id
        if kind == "character":
            pos = obj.pos
            return f"{obj.name}身边", lambda p: p != pos and chebyshev(p, pos) <= 1
        return f"{obj.name}旁边", lambda p: chebyshev(p, obj.pos) <= 1

    def _interrupt(self, c: Character) -> str | None:
        """取消寻路与进行中的交互，返回描述；本来空闲时返回 None。"""
        parts = []
        if c.path:
            parts.append("你停下了脚步")
            c.path, c.dest, c.pending, c.move_acc = [], None, None, 0.0
        job = c.job
        if job:
            c.job = None
            act = job.action
            if act.cumulative:
                self.progress[job.key] = job.progress
            self._emit("job_end", cid=c.id, interactable=job.interactable.id, action=act.id,
                       completed=False, progress=job.progress)
            kept = "，进度会保留" if act.cumulative else ""
            parts.append(f"你停止了{act.label}（已进行 {job.progress:.0f}/{act.duration_s:.0f} 秒{kept}）")
        return join_describe(parts) or None

    def _walk(self, c: Character, path: list[Pos], dest: str, pending=None) -> str:
        """开始沿新路径行走。改道不算打断，只报告被打断的交互，返回其描述前缀。"""
        c.path, c.dest, c.pending = [], None, None
        interrupted = self._interrupt(c)
        c.path, c.dest, c.pending = path, dest, pending
        return f"{interrupted}。" if interrupted else ""

    def _arrive(self, c: Character) -> None:
        dest, pending = c.dest, c.pending
        c.dest, c.pending, c.move_acc = None, None, 0.0
        text = f"你到达了{dest}"
        if pending:
            it, act = pending
            text += f"，{self.interact(c.id, it.id, act.id)}"
        self._perceive(c, "arrived", text, True)

    def _resolve_pos(self, c: Character, goal: Pos) -> tuple[str, Callable[[Pos], bool]]:
        """坐标目的地；不可站立时（例如可交互点所在格）改为其旁最近的可站立格。"""
        if self._walkable(c, goal):
            return self.place_name(goal), lambda p: p == goal
        it = next((i for i in self.scenario.interactables if i.pos == goal), None)
        label = f"{it.name}旁边" if it else f"{self.place_name(goal)}附近"
        return label, lambda p: chebyshev(p, goal) <= 1

    def _flush_visibility(self) -> None:
        """把已确认的进出视野变化写成 enter / leave 感知。"""
        t = self.now()
        for key, (was, t0) in list(self._vis_pending.items()):
            if t - t0 < VIS_SETTLE_S:
                continue
            del self._vis_pending[key]
            o, c = self.characters[key[0]], self.characters[key[1]]
            place = self.place_name(o.pos)
            if was:
                self._perceive(o, "leave", f"{c.name}（{c.role}）离开了{place}", True, who=c.id)
            else:
                self._perceive(o, "enter", f"{c.name}（{c.role}）来到了{place}", True, who=c.id)

    def _move(self, c: Character, nxt: Pos) -> None:
        old = c.pos
        seen_before = {o.id for o in self.characters.values() if o is not c and self._sees(o.pos, old)}
        c.pos = nxt
        c.last_step_t = self.now()
        self._emit("moved", cid=c.id, pos=list(nxt), room=self.grid.room_at(nxt))
        for o in self.characters.values():
            if o is c:
                continue
            was, now = o.id in seen_before, self._sees(o.pos, nxt)
            if was != now:
                # 视野变化先暂存，确认期内又变回原状时成对抵消
                key = (o.id, c.id)
                if key in self._vis_pending:
                    del self._vis_pending[key]
                else:
                    self._vis_pending[key] = (was, self.now())
            elif not now:
                self._footstep(o, c, old)

    def _footstep(self, listener: Character, mover: Character, old: Pos) -> None:
        """其他房间的人走近到一定声音代价以内时，听者听到脚步声。"""
        cost = self.grid.sound_cost(mover.pos, listener.pos)
        if cost is None or cost > FOOTSTEP_COST:
            return
        before = self.grid.sound_cost(old, listener.pos)
        if before is not None and before <= cost:
            return
        key, t = (listener.id, mover.id), self.now()
        if t - self._footsteps.get(key, -math.inf) < FOOTSTEP_REPEAT_S:
            return
        self._footsteps[key] = t
        where = self._where(listener, mover.pos)
        self._perceive(listener, "footsteps", f"{where}传来脚步声，越来越近", True, direction=where)

    def _where(self, listener: Character, src: Pos) -> str:
        """声源相对听者的方位描述，例如「东边（电梯厅）」。"""
        d = direction(listener.pos, src)
        room = self.grid.room_at(src)
        if room is None or self._sees(listener.pos, src):
            return d
        return f"{d}（{self.scenario.rooms[room].name}）"

    def _hearers(self, src: Character, volume: str) -> list[tuple[Character, str, float]]:
        out = []
        costs = self.grid.sound_costs(src.pos)
        for o in self.characters.values():
            if o is not src:
                level, gain = sound_level(costs.get(o.pos), volume)
                if level:
                    out.append((o, level, round(gain, 3)))
        return out

    def _hear_sentence(self, c: Character, sentence: str, volume: str, speech_id: int | None) -> None:
        place = self.place_name(c.pos)
        hearers = {}
        for o, level, gain in self._hearers(c, volume):
            hearers[o.id] = {"level": level, "gain": gain}
            data = {"speaker": c.id, "volume": volume, "level": level, "gain": gain, "speech_id": speech_id}
            if level == CLEAR:
                self._perceive(o, "speech", f"{c.name}在{place}{_SPEAK_VERB[volume]}：「{sentence}」", True,
                               name=c.name, place=place, sentence=sentence, **data)
            else:
                d = direction(o.pos, c.pos)
                if self._sees(o.pos, c.pos):
                    text = f"{c.name}在{d}{_MUFFLED_VERB[volume]}，听不清内容"
                else:
                    text = f"{self._where(o, c.pos)}有人在{_MUFFLED_VERB[volume]}，听不清内容"
                    data["speaker"] = None
                self._perceive(o, "muffled", text, False, **data)
        if c.id in self.recordings:   # 录音笔也录下玩家自己的声音
            p = Percept(self.now(), "speech", f"你说：「{sentence}」", False,
                        {"speaker": c.id, "sentence": sentence, "volume": volume, "self": True})
            self.recordings[c.id].append(p)
        self._emit("speech", cid=c.id, speech_id=speech_id, sentence=sentence, volume=volume,
                   pos=list(c.pos), hearers=hearers)

    def _emit_sound(self, c: Character, act: Action) -> None:
        s = act.sound
        hearers = {}
        for o, level, gain in self._hearers(c, s.volume):
            hearers[o.id] = {"level": level, "gain": gain}
            if level == CLEAR:
                text = f"{self._where(o, c.pos)}传来{s.text}"
            else:
                text = f"隐约听到{direction(o.pos, c.pos)}传来{s.text}"
            self._perceive(o, "sound", text, False, level=level, gain=gain, sound=s.text)
        self._emit("sound", cid=c.id, interactable=c.job.interactable.id, action=act.id,
                   text=s.text, volume=s.volume, pos=list(c.pos), hearers=hearers)

    def _advance_job(self, c: Character, dt: float) -> bool:
        """推进交互，完成时结算效果并返回 True。"""
        job = c.job
        act = job.action
        job.progress += dt
        if act.sound and dt:
            job.sound_acc += dt
            while job.sound_acc >= act.sound.every_s:
                job.sound_acc -= act.sound.every_s
                if job.progress < act.duration_s:
                    self._emit_sound(c, act)
        if job.progress < act.duration_s:
            return False
        c.job = None
        self.progress.pop(job.key, None)
        effect = act.on_complete
        for k, v in effect.set.items():
            old = self.flags.get(k)
            self.flags[k] = v
            if old != v:
                self._emit("flag", key=k, value=v, old=old)
        self._emit("job_end", cid=c.id, interactable=job.interactable.id, action=act.id,
                   completed=True, progress=job.progress)
        data = {"interactable": job.interactable.id, "action": act.id}
        self._perceive(c, "job_done", f"你完成了{act.label}", True, **data)
        if effect.reveal:
            self._perceive(c, "reveal", effect.reveal, True, **data)
        for cid, text in effect.notify.items():
            self._perceive(self.characters[cid], "notify", text, True, **data)
        return True

    def _advance_sim(self, c: Character, dt: float) -> None:
        """模拟语音播放：每字 SIM_CHAR_S 秒，走与真实后端相同的回调。"""
        sp = c.speech
        sp._left -= dt
        while c.speech is sp and sp._left <= 0:
            if sp._next < len(sp.sentences):
                sentence = sp.sentences[sp._next]
                sp._next += 1
                sp._left += len(sentence) * SIM_CHAR_S
                self.speech_sentence(c.id, sp.id, sentence)
            else:
                self.speech_finished(c.id, sp.id, aborted=False)

    def _abort_speech(self, c: Character) -> Speech:
        sp = c.speech
        self.speech_finished(c.id, sp.id, aborted=True)
        if self.voice is not None:
            self.voice.abort(sp)
        return sp


def _exact(query: str, items: list[tuple[str, str, object]]):
    """按 id 或名称完全一致匹配。"""
    q = query.strip()
    return next((obj for id_, name, obj in items if q in (id_, name)), None)


def _match(query: str, items: list[tuple[str, str, object]]):
    """按 id 或名称精确匹配；否则在名称中做唯一的包含匹配。"""
    q = query.strip()
    for id_, name, obj in items:
        if q in (id_, name):
            return obj
    partial = [obj for _, name, obj in items if q and (q in name or name in q)]
    return partial[0] if len(partial) == 1 else None
