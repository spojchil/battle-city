import pytest

from linx_game.engine.grid import direction
from linx_game.engine.sound import audibility


@pytest.mark.parametrize("cost, volume, level, gain", [
    (0, "正常", "clear", 1.0),
    (7, "正常", "clear", 0.5),
    (11, "正常", "muffled", 0.1),
    (9, "正常", "muffled", 0.3),
    (12, "正常", None, 0.0),
    (2, "耳语", "clear", 0.5),
    (3, "耳语", "muffled", 0.1),
    (4, "耳语", None, 0.0),
    (16, "大喊", "clear", 0.5),
    (24, "大喊", "muffled", 0.1),
    (None, "大喊", None, 0.0),
])
def test_audibility_levels(cost, volume, level, gain):
    got_level, got_gain = audibility(cost, volume)
    assert got_level == level
    assert got_gain == pytest.approx(gain)


def test_door_attenuation(h):
    grid = h.world.grid
    # 同房间直线 2 格：代价 2；隔一扇门的 2 格：地板 1 + 门 4 = 5
    assert grid.sound_cost((6, 1), (8, 1)) == 2
    assert grid.sound_cost((8, 1), (10, 1)) == 5
    assert grid.sound_cost((10, 1), (8, 1)) == 5
    # 受限门不挡声音
    assert grid.sound_cost((5, 3), (5, 5)) == 5


def test_offstage_sound_isolated(h):
    grid = h.world.grid
    assert grid.sound_cost((0, 2), (2, 2)) is None
    assert grid.sound_cost((2, 2), (0, 2)) is None
    assert grid.sound_cost((0, 1), (0, 3)) == 2   # 场外房间内部照常传播


def test_direction():
    assert direction((0, 0), (3, 0)) == "东边"
    assert direction((0, 0), (0, 3)) == "南边"
    assert direction((5, 5), (2, 2)) == "西北边"
    assert direction((1, 1), (1, 1)) == "身边"


def test_pathfinding_respects_walls_and_doors(h):
    grid = h.world.grid
    path = grid.find_path((3, 2), lambda p: p == (11, 2), lambda p: grid.walkable(p, "player"))
    assert path[-1] == (11, 2)
    assert (9, 1) in path                          # 只能经过门
    assert all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in zip([(3, 2)] + path, path))
    assert len(path) == 10                         # 四邻接最短路
    assert not grid.walkable((2, 1), "player")     # 可交互点所在格不可通行
