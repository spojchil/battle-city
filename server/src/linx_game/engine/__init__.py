"""语音推理游戏的通用引擎：剧本、地图、声音、世界状态与感知。

引擎与剧本内容分离，不包含任何具体剧情。
"""

from .perception import Percept, render
from .scenario import Scenario, load_scenario, parse_scenario
from .world import Character, Event, Speech, VoiceBackend, World

__all__ = [
    "Character", "Event", "Percept", "Scenario", "Speech", "VoiceBackend", "World",
    "load_scenario", "parse_scenario", "render",
]
