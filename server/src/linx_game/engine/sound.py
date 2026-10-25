"""可闻度：由声音路径代价与音量计算听清程度与增益。

玩家与 AI 角色、AI 角色之间都遵循同一套规则。
"""

CLEAR = "clear"
MUFFLED = "muffled"

# 音量 -> (清晰上限, 模糊上限)，单位为声音路径代价（地板一格 1，门格 4）
VOLUMES = {"耳语": (2, 3), "正常": (7, 11), "大喊": (16, 24)}


def audibility(cost: float | None, volume: str) -> tuple[str | None, float]:
    """返回 (等级, 增益)。等级为 clear / muffled，听不到时为 None。

    增益在清晰范围内从 1 线性降到 0.5，在模糊范围内从 0.5 线性降到 0.1。
    """
    if cost is None:
        return None, 0.0
    clear, muffled = VOLUMES[volume]
    if cost <= clear:
        return CLEAR, 1 - 0.5 * cost / clear
    if cost <= muffled:
        return MUFFLED, 0.5 - 0.4 * (cost - clear) / (muffled - clear)
    return None, 0.0
