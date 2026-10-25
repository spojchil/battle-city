"""多说话人识别检查。

在同一会话中让各测试音色轮流发言，要求智能体调用 record_speech 记录，
对照 MCP 服务日志中的 speaker 参数，评估声纹识别结果能否传递到工具调用。
"""

import argparse
import asyncio
import json

from dotenv import find_dotenv, load_dotenv

from linx_game.devtools import tts
from linx_game.devtools.enroll import SPEAKERS
from linx_game.devtools.vdevice import VirtualDevice

LINES = ["我觉得它是圆的", "它通常是红色的", "很多人早餐会吃它", "它长在树上"]


async def _run(rounds: int) -> None:
    load_dotenv(find_dotenv(usecwd=True))
    async with VirtualDevice.from_ota() as dev:
        print(f"session={dev.session_id}")
        for r in range(rounds):
            for i, (voice, name) in enumerate(SPEAKERS.items()):
                line = LINES[(i + r) % len(LINES)]
                turn = await dev.say(tts.synthesize(f"请帮我记录发言：{line}", voice=voice))
                print(json.dumps(
                    {"expected": name, "voice": voice, "stt": turn.stt, "reply": "".join(turn.replies)},
                    ensure_ascii=False,
                ))


def main() -> None:
    parser = argparse.ArgumentParser(description="多说话人识别检查")
    parser.add_argument("--rounds", type=int, default=1)
    asyncio.run(_run(parser.parse_args().rounds))


if __name__ == "__main__":
    main()
