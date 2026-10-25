import asyncio
from pathlib import Path
from types import SimpleNamespace

from linx_game.engine import World, load_scenario
from linx_game.runtime.brain import Brain

MINI = Path(__file__).parent / "engine" / "fixtures" / "mini.yaml"


def test_reply_window_defers_minor_wakes():
    async def main():
        brain = Brain(World(load_scenario(MINI)), "guest", SimpleNamespace())
        brain.hold(0.3)
        brain.wake(urgent=False)                 # 小动静：窗口内不叫醒
        await asyncio.sleep(0.1)
        assert not brain._wake.is_set()
        await asyncio.sleep(0.3)
        assert brain._wake.is_set()              # 窗口过了再醒

        brain._wake.clear()
        brain.hold(0.3)
        brain.wake(urgent=True)                  # 听到有人说话：立即醒
        assert brain._wake.is_set()

        brain._wake.clear()
        brain.wake(urgent=False)                 # 窗口未过：等窗口结束
        assert not brain._wake.is_set()
        await asyncio.sleep(0.4)
        assert brain._wake.is_set()
    asyncio.run(main())
