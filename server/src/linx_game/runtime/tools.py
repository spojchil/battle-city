"""AI 角色的行动工具（MCP）。

角色只能通过这些工具影响世界。工具只表达意图，由世界裁决并立即返回结果；
移动、交互、说话都需要时间，进展与结果以感知的形式在后续轮次送达。
调用方由灵矽注入的 X-Linx-Device-Id 识别，对应某个角色的 brain 连接。
"""

import logging
from typing import Literal

from mcp.server.mcpserver import Context, MCPServer

from linx_game.engine.world import World

log = logging.getLogger("linx_game.tools")

mcp = MCPServer("linx-game")

# 设备 MAC（大写）→ (世界, 角色 id)，由运行时在开局时登记
actors: dict[str, tuple[World, str]] = {}


def _actor(ctx: Context) -> tuple[World, str] | None:
    request = ctx.request_context.request
    device = request.headers.get("x-linx-device-id", "").upper() if request is not None else ""
    return actors.get(device)


def _act(ctx: Context, name: str, **args) -> str:
    actor = _actor(ctx)
    if actor is None:
        return "当前没有进行中的游戏。"
    world, cid = actor
    result = getattr(world, name)(cid, **args)
    if name == "say":
        result = "开始说话。"
    log.info("tool %s %s %s -> %s", cid, name, args, result)
    return result


@mcp.tool(description="说一句话。这是发出声音的唯一方式，不调用就没有人听见。说话需要时间，"
                      "期间可以继续行动；新的 say 会打断你还没说完的话。只有附近的人能听到，"
                      "距离越远越听不清。volume：耳语、正常、大喊。")
def say(ctx: Context, text: str, volume: Literal["耳语", "正常", "大喊"] = "正常") -> str:
    return _act(ctx, "say", text=text, volume=volume)


@mcp.tool(description="立即停止你正在说的话，例如不该听到的人走近时。已经说出口的部分无法收回。")
def stop_speaking(ctx: Context) -> str:
    return _act(ctx, "stop_speaking")


@mcp.tool(description="走向某个地点、某个人或某个可交互点。target 使用感知中出现的名称。"
                      "移动需要时间，途中可以继续说话；到达后会收到新的感知。")
def move_to(ctx: Context, target: str) -> str:
    return _act(ctx, "move_to", target=target)


@mcp.tool(description="对身边一格内的可交互点执行一个行动。target 为可交互点名称，action 为感知中列出的行动名称。"
                      "行动持续一段时间，完成后会收到结果；移动或 stop 会中断行动。")
def interact(ctx: Context, target: str, action: str) -> str:
    return _act(ctx, "interact", target=target, action=action)


@mcp.tool(description="停下：停止移动或正在进行的行动。")
def stop(ctx: Context) -> str:
    return _act(ctx, "stop")
