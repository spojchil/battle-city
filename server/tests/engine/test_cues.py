import pytest

from linx_game.engine import World
from linx_game.engine.scenario import parse_scenario


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _world(mini_data, cues):
    clock = FakeClock()
    world = World(parse_scenario({**mini_data, "cues": cues}), clock=clock)
    return world, clock


def _advance(world, clock, seconds, dt=0.1):
    for _ in range(round(seconds / dt)):
        clock.t += dt
        world.tick(dt)


def test_cue_fires_once_at_clock_time(mini_data):
    world, clock = _world(mini_data, [{"to": "worker", "at": "14:01", "text": "手机闹钟响了：该去巡查了。"}])
    _advance(world, clock, 59)
    assert not [p for p in world.drain("worker") if p.kind == "cue"]
    _advance(world, clock, 2)
    cues = [p for p in world.drain("worker") if p.kind == "cue"]
    assert [p.text for p in cues] == ["手机闹钟响了：该去巡查了。"] and cues[0].wake
    _advance(world, clock, 5)
    assert not [p for p in world.drain("worker") if p.kind == "cue"]


def test_cue_waits_for_flags(mini_data):
    world, clock = _world(mini_data, [{"to": "guard", "when": {"lamp": "on"}, "text": "灯亮了。"}])
    _advance(world, clock, 1)
    assert not [p for p in world.drain("guard") if p.kind == "cue"]
    world.flags["lamp"] = "on"
    _advance(world, clock, 0.2)
    assert [p.text for p in world.drain("guard") if p.kind == "cue"] == ["灯亮了。"]


@pytest.mark.parametrize("cue, message", [
    ({"to": "nobody", "at": "14:01", "text": "x"}, "未知角色"),
    ({"to": "worker", "at": "2pm", "text": "x"}, "HH:MM"),
    ({"to": "worker", "text": "x"}, "至少需要一个"),
])
def test_cue_validation(mini_data, cue, message):
    with pytest.raises(ValueError, match=message):
        parse_scenario({**mini_data, "cues": [cue]})
