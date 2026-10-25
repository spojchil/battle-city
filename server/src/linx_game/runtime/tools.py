"""AI 角色的行动工具（MCP）。

角色只能通过这些工具影响世界。工具只表达意图，由世界裁决并立即返回结果；
移动、交互、说话都需要时间，进展与结果以感知的形式在后续轮次送达。
调用方由灵矽注入的 X-Linx-Device-Id 识别，对应某个角色的 brain 连接。
工具均为异步函数，在事件循环中执行，以便世界状态只在同一线程中修改。
"""

import logging
from typing import TYPE_CHECKING, Literal

from mcp.server.mcpserver import Context, MCPServer

from linx_game.engine.world import World

if TYPE_CHECKING:
    from linx_game.runtime.brain import Brain

log = logging.getLogger("linx_game.tools")

mcp = MCPServer("linx-game")

# 设备 MAC（大写）→ (世界, 角色 id, 思考循环)，由运行时在开局时登记；
# 工具执行结果交给角色的思考循环，写入其记忆
actors: dict[str, tuple[World, str, "Brain"]] = {}


def _actor(ctx: Context) -> tuple[World, str, "Brain"] | None:
    request = ctx.request_context.request
    device = request.headers.get("x-linx-device-id", "").upper() if request is not None else ""
    return actors.get(device)


def _act(ctx: Context, name: str, **args) -> str:
    actor = _actor(ctx)
    if actor is None:
        return "当前没有进行中的游戏。"
    world, cid, brain = actor
    result = getattr(world, name)(cid, **args)
    if name == "say":
        result = "开始说话。"
    brain.record_result(result)
    log.info("tool %s %s %s -> %s", cid, name, args, result)
    return result


@mcp.tool(description="说一句话。这是发出声音的唯一方式，不调用就没有人听见。说话需要时间，"
                      "期间可以继续行动；新的 say 会打断你还没说完的话。只有附近的人能听到，"
                      "距离越远越听不清。对某人说完后，给对方留出回答的时间，不要接着说；"
                      "别人正在交谈时不要插话，除非话是对你说的。volume：耳语、正常、大喊。")
async def say(ctx: Context, text: str, volume: Literal["耳语", "正常", "大喊"] = "正常") -> str:
    return _act(ctx, "say", text=text, volume=volume)


@mcp.tool(description="立即停止你正在说的话，例如不该听到的人走近时。已经说出口的部分无法收回。")
async def stop_speaking(ctx: Context) -> str:
    return _act(ctx, "stop_speaking")


@mcp.tool(description="走向某个地点、某个人或某个可交互点。target 使用感知中出现的名称。"
                      "移动需要时间，途中可以继续说话；到达后会收到新的感知。")
async def move_to(ctx: Context, target: str) -> str:
    return _act(ctx, "move_to", target=target)


@mcp.tool(description="对某个可交互点执行一个行动，不在身边时会先走过去。target 为可交互点名称，action 为感知中列出的行动名称。"
                      "行动持续一段时间，完成后会收到结果；移动或 stop 会中断行动。")
async def interact(ctx: Context, target: str, action: str) -> str:
    return _act(ctx, "interact", target=target, action=action)


@mcp.tool(description="中断：放弃正在进行的移动或行动（未完成的进度可能作废）。只在确实要改变主意时调用；"
                      "想继续手上的事或等待，请用 wait。")
async def stop(ctx: Context) -> str:
    return _act(ctx, "stop")


@mcp.tool(description="等待：什么都不改变，继续手上正在做的事。seconds 大于 0 时，到时间会再给你一次思考的机会"
                      "（例如「过一两分钟再进去」）；有新情况时会提前叫你。")
async def wait(ctx: Context, seconds: int = 0) -> str:
    actor = _actor(ctx)
    if actor is None:
        return "当前没有进行中的游戏。"
    _, cid, brain = actor
    seconds = max(0, min(int(seconds), 300))
    result = f"你等待约 {seconds} 秒。" if seconds else "你继续等待。"
    if seconds:
        brain.wake_after(seconds)
    brain.record_result(result)
    log.info("tool %s wait %s", cid, seconds)
    return result
