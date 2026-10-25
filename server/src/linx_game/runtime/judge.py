"""结局评判：以警方视角审阅玩家提交的录音与照片。"""

import json
import logging

import httpx

from linx_game.engine.world import World
from linx_game.llm_gateway import prepare, upstream

log = logging.getLogger("linx_game.judge")

PROMPT = """你是接到市民报案材料的值班警官。报案人提交了一段随身录音笔的录音记录和若干张手机照片，
你要判断这些材料是否足以让警方立即介入（提前到场排查、控制相关人员）。

只依据报案人提交的材料判断，不要假设材料之外的信息。下面的「真相」与「评判标准」只供你衡量材料的分量，
报案人并不知道这些内容。照片数量过多、与案情无关的照片过多，会降低照片整体的可信度。

## 真相
{truth}

## 评判标准
{criteria}

调用 verdict 工具给出结论：success 表示警方是否会提前介入；title 为一句结局标题；
summary 以警官口吻写两三句结论；points 列出起作用的关键证据与缺失的环节。"""

VERDICT_TOOL = {
    "type": "function",
    "function": {
        "name": "verdict",
        "description": "给出评判结论",
        "parameters": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "points": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["success", "title", "summary", "points"],
        },
    },
}


def evidence(world: World, cid: str) -> str:
    lines = ["## 录音记录"]
    lines += [f"- {world.clock_text(p.t)} {p.text}" for p in world.recordings.get(cid, [])] or ["（无）"]
    lines.append("## 照片")
    for i, photo in enumerate(world.photos.get(cid, []), 1):
        lines.append(f"### 照片 {i}\n{photo.get('text') or json.dumps(photo, ensure_ascii=False)}")
    if not world.photos.get(cid):
        lines.append("（无）")
    return "\n".join(lines)


async def judge(world: World, cid: str) -> dict:
    scenario = world.scenario
    body = prepare({
        "model": "moonshotai/kimi-k3-free",
        "messages": [
            {"role": "system", "content": PROMPT.format(truth=scenario.judge_truth, criteria=scenario.judge_criteria)},
            {"role": "user", "content": evidence(world, cid)},
        ],
        "tools": [VERDICT_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "verdict"}},
    })
    base_url, api_key = upstream()
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=90) as client:
            resp = await client.post("/chat/completions", json=body, headers={"Authorization": f"Bearer {api_key}"})
        call = resp.json()["choices"][0]["message"]["tool_calls"][0]
        result = json.loads(call["function"]["arguments"])
    except Exception:
        log.exception("judge failed")
        return {"success": False, "title": "评判失败", "summary": "评判服务暂时不可用。", "points": []}
    log.info("verdict %s", json.dumps(result, ensure_ascii=False))
    return result
