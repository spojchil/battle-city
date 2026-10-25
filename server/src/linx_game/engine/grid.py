"""地图网格：通行判断、寻路与声音传播路径。

坐标 (x, y)，x 为列、y 为行，左上角 (0, 0)，一格约 1 米。
"""

import heapq
import math
from collections import deque
from collections.abc import Callable

from .scenario import Cell, Pos, Scenario

DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))

# 声音经过一格的代价
FLOOR_COST = 1
DOOR_COST = 4

_COMPASS = ("东", "东南", "南", "西南", "西", "西北", "北", "东北")


def chebyshev(a: Pos, b: Pos) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def direction(src: Pos, dst: Pos) -> str:
    """dst 相对于 src 的方位（八方位，y 轴向下为南）。"""
    dx, dy = dst[0] - src[0], dst[1] - src[1]
    if dx == 0 and dy == 0:
        return "身边"
    angle = math.degrees(math.atan2(dy, dx)) % 360
    return _COMPASS[int((angle + 22.5) // 45) % 8] + "边"


class Grid:
    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.width, self.height = scenario.width, scenario.height
        self.rooms = scenario.rooms
        self.blocked = {it.pos for it in scenario.interactables}   # 可交互点所在格不可通行
        self._sound_cache: dict[Pos, dict[Pos, float]] = {}

    def cell(self, pos: Pos) -> Cell | None:
        return self.scenario.cell(pos)

    def room_at(self, pos: Pos) -> str | None:
        """所在房间 id；门格、墙与地图外为 None。"""
        cell = self.cell(pos)
        return cell.room if cell and cell.kind == "floor" else None

    def is_door(self, pos: Pos) -> bool:
        cell = self.cell(pos)
        return cell is not None and cell.kind == "door"

    def is_offstage(self, pos: Pos) -> bool:
        room = self.room_at(pos)
        return room is not None and self.rooms[room].offstage

    def door_rooms(self, pos: Pos) -> list[str]:
        """门格相邻的场内房间，按剧本中房间的定义顺序排列。"""
        rooms = {self.room_at((pos[0] + dx, pos[1] + dy)) for dx, dy in DIRS}
        return [r for r in self.rooms if r in rooms and not self.rooms[r].offstage]

    def walkable(self, pos: Pos, cid: str | None = None, player: bool = False) -> bool:
        """角色 cid 能否站上该格。玩家不能进入场外房间；受限门只放行列出的角色。"""
        cell = self.cell(pos)
        if cell is None or pos in self.blocked:
            return False
        if cell.kind == "floor":
            return not (player and self.rooms[cell.room].offstage)
        if cell.kind == "door":
            return cell.allowed is None or cid in cell.allowed
        return False

    def find_path(self, start: Pos, is_goal: Callable[[Pos], bool],
                  can_walk: Callable[[Pos], bool]) -> list[Pos] | None:
        """四邻接广度优先寻路，返回不含起点的步序列；起点即目标时返回空列表，不可达返回 None。"""
        if is_goal(start):
            return []
        prev = {start: None}
        queue = deque([start])
        while queue:
            cur = queue.popleft()
            for dx, dy in DIRS:
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt in prev or not can_walk(nxt):
                    continue
                prev[nxt] = cur
                if is_goal(nxt):
                    path = [nxt]
                    while prev[path[-1]] != start:
                        path.append(prev[path[-1]])
                    return path[::-1]
                queue.append(nxt)
        return None

    def sound_costs(self, origin: Pos) -> dict[Pos, float]:
        """从 origin 出发到各格的声音路径代价。

        声音经过地板格与所有门格（不论 allowed）。相邻两格之间的代价取两格权重的平均，
        穿过一扇门（地板-门-地板）共计 5，与「地板 1、门格 4」一致，且代价对称。
        场外房间与场内互不相通：从场外出发只在该房间内传播，从场内出发不进入场外房间。
        """
        if origin in self._sound_cache:
            return self._sound_cache[origin]
        origin_room = self.room_at(origin)
        if origin_room and self.rooms[origin_room].offstage:
            def passes(p):
                return self.room_at(p) == origin_room
        else:
            def passes(p):
                cell = self.cell(p)
                return cell is not None and (cell.kind == "door" or (cell.kind == "floor" and not self.is_offstage(p)))

        def weight(p):
            return DOOR_COST if self.is_door(p) else FLOOR_COST

        costs = {origin: 0.0}
        heap = [(0.0, origin)]
        while heap:
            cost, cur = heapq.heappop(heap)
            if cost > costs[cur]:
                continue
            for dx, dy in DIRS:
                nxt = (cur[0] + dx, cur[1] + dy)
                if not passes(nxt):
                    continue
                c = cost + (weight(cur) + weight(nxt)) / 2
                if c < costs.get(nxt, math.inf):
                    costs[nxt] = c
                    heapq.heappush(heap, (c, nxt))
        self._sound_cache[origin] = costs
        return costs

    def sound_cost(self, a: Pos, b: Pos) -> float | None:
        """a 到 b 的声音路径代价，不相通时为 None。"""
        return self.sound_costs(a).get(b)
