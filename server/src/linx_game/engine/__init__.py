"""语音推理游戏的通用引擎：剧本、地图、声音、世界状态与感知。

引擎与剧本内容分离，不包含任何具体剧情。
"""

from .scenario import Scenario, load_scenario, parse_scenario

__all__ = ["Scenario", "load_scenario", "parse_scenario"]
