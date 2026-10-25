"""剧本加载与校验。

剧本是 YAML 文件（`scenarios/<id>/scenario.yaml`），描述地图、房间、可交互点与角色。
本模块只做结构解析与校验，不含任何具体剧情。校验失败抛 ValueError，信息指明出错字段。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .sound import VOLUMES

Pos = tuple[int, int]

CELL_KINDS = ("wall", "void", "floor", "door")

_MISSING = object()
_TYPE_NAMES = {str: "字符串", int: "整数", float: "数字", dict: "映射", list: "列表", bool: "布尔值"}


@dataclass(frozen=True)
class Cell:
    kind: str
    room: str | None = None
    allowed: tuple[str, ...] | None = None   # 门格：只有列出的角色能通过；None 表示不限
    see_through_when: dict | None = None     # 门格：标志全部匹配时门虚掩，相邻格的角色能看见门另一侧


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    offstage: bool = False


@dataclass(frozen=True)
class SoundSpec:
    text: str
    volume: str
    every_s: float


@dataclass(frozen=True)
class Effect:
    set: dict = field(default_factory=dict)
    reveal: str = ""
    notify: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    seen_as: str
    duration_s: float
    who: tuple[str, ...] | None
    requires: dict
    cumulative: bool
    sound: SoundSpec | None
    on_complete: Effect


@dataclass(frozen=True)
class Describe:
    when: dict
    text: str


@dataclass(frozen=True)
class Interactable:
    id: str
    name: str
    pos: Pos
    room: str
    flags: dict
    describe: tuple[Describe, ...]
    actions: tuple[Action, ...]


@dataclass(frozen=True)
class CharacterSpec:
    id: str
    name: str
    role: str
    voice: str
    start: Pos
    player: bool
    card: str
    task: str


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    intro: str
    duration_s: float
    start_clock: str
    start_clock_s: int          # start_clock 换算成当日秒数
    legend: dict[str, Cell]
    rows: tuple[str, ...]
    rooms: dict[str, Room]
    interactables: tuple[Interactable, ...]
    characters: tuple[CharacterSpec, ...]
    judge_truth: str = ""
    judge_criteria: str = ""

    @property
    def width(self) -> int:
        return len(self.rows[0])

    @property
    def height(self) -> int:
        return len(self.rows)

    def cell(self, pos: Pos) -> Cell | None:
        x, y = pos
        if 0 <= y < self.height and 0 <= x < self.width:
            return self.legend[self.rows[y][x]]
        return None


def load_scenario(path) -> Scenario:
    """读取并校验剧本文件。"""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: YAML 解析失败：{e}") from e
    try:
        return parse_scenario(data)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from e


def parse_scenario(data) -> Scenario:
    """校验已解析的剧本数据并构造 Scenario。"""
    if not isinstance(data, dict):
        raise ValueError("剧本顶层应为映射")

    rooms = {}
    for rid, r in _get(data, "rooms", "", dict).items():
        where = f"rooms.{rid}"
        r = _as(r, where, dict)
        rooms[str(rid)] = Room(str(rid), _get(r, "name", where, str), _get(r, "offstage", where, bool, False))

    legend, rows = _parse_map(_get(data, "map", "", dict), rooms)
    char_ids = [str(_get(_as(c, f"characters[{i}]", dict), "id", f"characters[{i}]", str))
                for i, c in enumerate(_get(data, "characters", "", list))]
    _check_unique(char_ids, "characters", "id")
    known = set(char_ids)
    for ch, cell in legend.items():
        for cid in cell.allowed or ():
            if cid not in known:
                raise ValueError(f"map.legend.{ch!r}.allowed: 未知角色 {cid!r}")

    def cell_at(pos):
        x, y = pos
        if 0 <= y < len(rows) and 0 <= x < len(rows[0]):
            return legend[rows[y][x]]
        return None

    interactables = [_parse_interactable(it, f"interactables[{i}]", cell_at, known)
                     for i, it in enumerate(_get(data, "interactables", "", list, []))]
    _check_unique([it.id for it in interactables], "interactables", "id")
    _check_unique([it.pos for it in interactables], "interactables", "pos")
    blocked = {it.pos for it in interactables}

    characters = []
    for i, c in enumerate(data["characters"]):
        where = f"characters[{i}]"
        start = _pos(_get(c, "start", where, list), f"{where}.start")
        cell = cell_at(start)
        if cell is None or cell.kind not in ("floor", "door") or start in blocked:
            raise ValueError(f"{where}.start: {start} 不是可通行格")
        characters.append(CharacterSpec(
            id=char_ids[i], name=_get(c, "name", where, str), role=_get(c, "role", where, str, ""),
            voice=_get(c, "voice", where, str, ""), start=start,
            player=_get(c, "player", where, bool, False),
            card=_get(c, "card", where, str, ""), task=_get(c, "task", where, str, ""),
        ))

    start_clock = str(_get(data, "start_clock", "", str))
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", start_clock)
    if not m or int(m[1]) > 23 or int(m[2]) > 59 or int(m[3] or 0) > 59:
        raise ValueError(f"start_clock: 应为 HH:MM 或 HH:MM:SS，实际为 {start_clock!r}")
    duration = _get(data, "duration_s", "", (int, float))
    if duration <= 0:
        raise ValueError(f"duration_s: 应为正数，实际为 {duration!r}")
    judge = _get(data, "judge", "", dict, {})

    return Scenario(
        id=str(_get(data, "id", "", str)), title=_get(data, "title", "", str),
        intro=_get(data, "intro", "", str, ""), duration_s=duration, start_clock=start_clock,
        start_clock_s=int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3] or 0),
        legend=legend, rows=rows, rooms=rooms,
        interactables=tuple(interactables), characters=tuple(characters),
        judge_truth=_get(judge, "truth", "judge", str, ""),
        judge_criteria=_get(judge, "criteria", "judge", str, ""),
    )


def _parse_map(m: dict, rooms: dict[str, Room]) -> tuple[dict[str, Cell], tuple[str, ...]]:
    legend = {}
    for ch, spec in _get(m, "legend", "map", dict).items():
        ch = str(ch)
        where = f"map.legend.{ch!r}"
        if len(ch) != 1:
            raise ValueError(f"{where}: 图例键应为单个字符")
        spec = _as(spec, where, dict)
        kind = _get(spec, "kind", where, str)
        if kind not in CELL_KINDS:
            raise ValueError(f"{where}.kind: 应为 {'/'.join(CELL_KINDS)} 之一，实际为 {kind!r}")
        room = None
        if kind == "floor":
            room = str(_get(spec, "room", where, str))
            if room not in rooms:
                raise ValueError(f"{where}.room: 未在 rooms 中定义的房间 {room!r}")
        allowed = None
        if kind == "door" and "allowed" in spec:
            allowed = tuple(str(a) for a in _get(spec, "allowed", where, list))
        see_through = None
        if spec.get("see_through_when") is not None:
            if kind != "door":
                raise ValueError(f"{where}.see_through_when: 只允许用于 door 格")
            see_through = _get(spec, "see_through_when", where, dict)
        legend[ch] = Cell(kind, room, allowed, see_through)

    text = _get(m, "grid", "map", str)
    rows = text.split("\n")
    while rows and not rows[-1].strip():
        rows.pop()
    if not rows:
        raise ValueError("map.grid: 地图为空")
    for y, row in enumerate(rows):
        if len(row) != len(rows[0]):
            raise ValueError(f"map.grid: 第 {y} 行长度为 {len(row)}，与第 0 行的 {len(rows[0])} 不一致")
        for x, ch in enumerate(row):
            if ch not in legend:
                raise ValueError(f"map.grid: ({x}, {y}) 处的字符 {ch!r} 未在 legend 中定义")
    return legend, tuple(rows)


def _parse_interactable(it, where: str, cell_at, known: set[str]) -> Interactable:
    it = _as(it, where, dict)
    pos = _pos(_get(it, "pos", where, list), f"{where}.pos")
    cell = cell_at(pos)
    if cell is None or cell.kind != "floor":
        raise ValueError(f"{where}.pos: {pos} 应位于某个房间的地板格")

    describe = []
    for i, d in enumerate(_get(it, "describe", where, list, [])):
        w = f"{where}.describe[{i}]"
        d = _as(d, w, dict)
        describe.append(Describe(_get(d, "when", w, dict, {}), _get(d, "text", w, str)))

    actions = []
    for i, a in enumerate(_get(it, "actions", where, list, [])):
        actions.append(_parse_action(_as(a, f"{where}.actions[{i}]", dict), f"{where}.actions[{i}]", known))
    _check_unique([a.id for a in actions], f"{where}.actions", "id")

    return Interactable(
        id=str(_get(it, "id", where, str)), name=_get(it, "name", where, str), pos=pos, room=cell.room,
        flags=_get(it, "flags", where, dict, {}), describe=tuple(describe), actions=tuple(actions),
    )


def _parse_action(a: dict, where: str, known: set[str]) -> Action:
    duration = _get(a, "duration_s", where, (int, float))
    if duration < 0:
        raise ValueError(f"{where}.duration_s: 不能为负数")
    who = None
    if "who" in a:
        who = tuple(str(c) for c in _get(a, "who", where, list))
        for cid in who:
            if cid not in known:
                raise ValueError(f"{where}.who: 未知角色 {cid!r}")

    sound = None
    if a.get("sound") is not None:
        w = f"{where}.sound"
        s = _as(a["sound"], w, dict)
        volume = _get(s, "volume", w, str, "正常")
        if volume not in VOLUMES:
            raise ValueError(f"{w}.volume: 应为 {'/'.join(VOLUMES)} 之一，实际为 {volume!r}")
        every = _get(s, "every_s", w, (int, float))
        if every <= 0:
            raise ValueError(f"{w}.every_s: 应为正数")
        sound = SoundSpec(_get(s, "text", w, str), volume, every)

    w = f"{where}.on_complete"
    oc = _get(a, "on_complete", where, dict, {})
    notify = {str(k): v for k, v in _get(oc, "notify", w, dict, {}).items()}
    for cid, text in notify.items():
        if cid not in known:
            raise ValueError(f"{w}.notify: 未知角色 {cid!r}")
        if not isinstance(text, str):
            raise ValueError(f"{w}.notify.{cid}: 应为字符串")
    effect = Effect(set=_get(oc, "set", w, dict, {}), reveal=_get(oc, "reveal", w, str, ""), notify=notify)

    label = _get(a, "label", where, str)
    return Action(
        id=str(_get(a, "id", where, str)), label=label, seen_as=_get(a, "seen_as", where, str, f"在{label}"),
        duration_s=duration, who=who, requires=_get(a, "requires", where, dict, {}),
        cumulative=_get(a, "cumulative", where, bool, False), sound=sound, on_complete=effect,
    )


def _join(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _type_name(typ) -> str:
    if isinstance(typ, tuple):
        return "数字"
    return _TYPE_NAMES.get(typ, typ.__name__)


def _as(value, where: str, typ):
    if not isinstance(value, typ) or (typ is not bool and isinstance(value, bool)):
        raise ValueError(f"{where}: 应为{_type_name(typ)}，实际为 {value!r}")
    return value


def _get(d: dict, key: str, where: str, typ, default=_MISSING):
    """读取字段并检查类型；缺失且无默认值时报错。"""
    if d.get(key) is None:
        if default is _MISSING:
            raise ValueError(f"{_join(where, key)}: 缺少必填字段")
        return default
    return _as(d[key], _join(where, key), typ)


def _pos(value: list, where: str) -> Pos:
    if len(value) != 2 or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        raise ValueError(f"{where}: 应为 [x, y] 两个整数，实际为 {value!r}")
    return value[0], value[1]


def _check_unique(values: list, where: str, key: str) -> None:
    seen = set()
    for i, v in enumerate(values):
        if v in seen:
            raise ValueError(f"{where}[{i}].{key}: 重复的 {v!r}")
        seen.add(v)
