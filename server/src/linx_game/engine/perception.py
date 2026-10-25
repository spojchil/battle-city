"""感知：单条感知记录，以及给 AI 角色的感知文本渲染。"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .grid import chebyshev

if TYPE_CHECKING:
    from .world import World


@dataclass
class Percept:
    t: float          # 开局以来的秒数
    kind: str         # speech / muffled / enter / leave / footsteps / sound / reveal / notify / job_done / arrived
    text: str
    wake: bool        # 是否值得唤醒 AI 角色立即响应
    data: dict = field(default_factory=dict)


def render(world: "World", cid: str, percepts: list[Percept]) -> str:
    """生成给 AI 角色的一段感知文本：时间、位置、所见、自身状态与新的感知。"""
    c = world.characters[cid]
    lines = [f"【时间】{world.clock_text()}", f"【位置】你在{world.place_name(c.pos)}。"]

    others = world.visible_characters(cid)
    lines.append("【在场的人】" if others else "【在场的人】除你之外没有别人。")
    for o in others:
        doing = "，".join(filter(None, [world.activity(o), "正在说话" if o.speech else ""]))
        lines.append(f"- {o.name}（{o.role}）" + (f"：{doing}" if doing else ""))

    objects = world.visible_interactables(cid)
    if objects:
        lines.append("【可交互的东西】")
    for it in objects:
        desc = "；".join(world.describe(it))
        far = chebyshev(c.pos, it.pos) > 1
        lines.append(f"- {it.name}（{it.id}）" + (f"：{desc}" if desc else "") + ("（离你较远，需走近）" if far else ""))
        for a in it.actions:
            if not world.can_do(cid, a):
                continue
            done = world.progress.get((it.id, a.id), 0)
            extra = f"，已完成 {done:.0f} 秒" if done else ""
            lines.append(f"  · 可以{a.label}（行动 {a.id}，约需 {a.duration_s:.0f} 秒{extra}）")

    lines.append("【你的状态】")
    status = []
    if c.path:
        then = f"，到达后开始{c.pending[1].label}" if c.pending else ""
        status.append(f"正在前往{c.dest}（还有 {len(c.path)} 步）{then}")
    if c.job:
        status.append(f"正在{c.job.action.label}（{c.job.progress:.0f}/{c.job.action.duration_s:.0f} 秒）")
    if c.speech:
        sp = c.speech
        status.append(f"正在说「{sp.text}」，已说出「{sp.spoken_text}」")
    lines.extend(f"- {s}" for s in status or ["你现在没有在走动、做事或说话。"])

    lines.append("【新的感知】" if percepts else "【新的感知】无")
    for p in sorted(percepts, key=lambda p: p.t):
        lines.append(f"- [{world.clock_text(p.t)}] {p.text}")
    return "\n".join(lines)
