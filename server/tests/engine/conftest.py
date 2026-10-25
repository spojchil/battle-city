from pathlib import Path

import pytest
import yaml

from linx_game.engine import World, load_scenario

MINI = Path(__file__).parent / "fixtures" / "mini.yaml"


class FakeClock:
    """可注入的假时钟。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Harness:
    """测试用世界：假时钟、按固定步长推进、收集事件。"""

    def __init__(self):
        self.clock = FakeClock()
        self.world = World(load_scenario(MINI), clock=self.clock)
        self.events = []
        self.world.subscribe(self.events.append)

    def advance(self, seconds: float, dt: float = 0.05) -> None:
        for _ in range(round(seconds / dt)):
            self.clock.t += dt
            self.world.tick(dt)

    def place(self, cid: str, x: int, y: int) -> None:
        self.world.characters[cid].pos = (x, y)

    def drain(self, cid: str, kind: str | None = None) -> list:
        return [p for p in self.world.drain(cid) if kind is None or p.kind == kind]

    def events_of(self, kind: str) -> list:
        return [e for e in self.events if e.kind == kind]


@pytest.fixture
def h() -> Harness:
    return Harness()


@pytest.fixture
def mini_data() -> dict:
    return yaml.safe_load(MINI.read_text(encoding="utf-8"))
