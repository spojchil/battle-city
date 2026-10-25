import copy

import pytest
import yaml
from conftest import MINI

from linx_game.engine import load_scenario, parse_scenario


def test_load_fixture():
    s = load_scenario(MINI)
    assert (s.width, s.height) == (14, 8)
    assert s.start_clock_s == 14 * 3600
    assert s.rooms["street"].offstage
    assert s.legend["k"].allowed == ("worker",)
    safe = next(it for it in s.interactables if it.id == "safe")
    assert safe.room == "store"
    assert safe.actions[0].cumulative and safe.actions[0].sound.every_s == 5
    assert [c.id for c in s.characters if c.player] == ["player"]


def _broken(data: dict, mutate) -> str:
    data = copy.deepcopy(data)
    mutate(data)
    with pytest.raises(ValueError) as e:
        parse_scenario(data)
    return str(e.value)


@pytest.mark.parametrize("mutate, expected", [
    (lambda d: d.pop("title"), "title: 缺少必填字段"),
    (lambda d: d.update(duration_s="long"), "duration_s: 应为数字"),
    (lambda d: d.update(start_clock="25:00"), "start_clock"),
    (lambda d: d["map"].update(grid="###\n##\n"), "map.grid: 第 1 行长度为 2"),
    (lambda d: d["map"].update(grid="#?#\n"), "map.grid: (1, 0) 处的字符 '?'"),
    (lambda d: d["map"]["legend"].update({"x": {"kind": "lava"}}), "map.legend.'x'.kind"),
    (lambda d: d["map"]["legend"].update({"x": {"kind": "floor", "room": "attic"}}), "map.legend.'x'.room"),
    (lambda d: d["map"]["legend"]["k"].update(allowed=["ghost"]), "map.legend.'k'.allowed: 未知角色 'ghost'"),
    (lambda d: d["characters"][1].update(start=[0, 0]), "characters[1].start: (0, 0) 不是可通行格"),
    (lambda d: d["characters"][1].update(start=[2, 1]), "characters[1].start: (2, 1) 不是可通行格"),
    (lambda d: d["characters"][2].update(id="player"), "characters[2].id: 重复的 'player'"),
    (lambda d: d["interactables"][0].update(pos=[1, 1]), "interactables[0].pos: (1, 1) 应位于某个房间的地板格"),
    (lambda d: d["interactables"][1]["actions"][0].update(who=["nobody"]), "interactables[1].actions[0].who"),
    (lambda d: d["interactables"][1]["actions"][0]["sound"].update(volume="很响"),
     "interactables[1].actions[0].sound.volume"),
    (lambda d: d["interactables"][0]["actions"][0]["on_complete"]["notify"].update(nobody="x"),
     "interactables[0].actions[0].on_complete.notify: 未知角色 'nobody'"),
])
def test_validation_errors(mini_data, mutate, expected):
    assert expected in _broken(mini_data, mutate)


def test_load_reports_file_path(tmp_path):
    bad = tmp_path / "scenario.yaml"
    bad.write_text(yaml.safe_dump({"id": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="scenario.yaml: rooms: 缺少必填字段"):
        load_scenario(bad)
