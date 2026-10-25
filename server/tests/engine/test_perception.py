from linx_game.engine import render


def test_render_sections(h):
    w = h.world
    h.place("guest", 5, 2)
    w.say("guest", "你好。")
    w.move_to("player", "走廊")
    h.advance(0.5)
    text = render(w, "player", w.drain("player"))
    for fragment in [
        "【时间】14:00:00",
        "【位置】你在休息区。",
        "- 阿美（住客）：正在说话",
        "- 台灯（lamp）：台灯关着",
        "  · 可以打开台灯（行动 switch_on，约需 2 秒）",
        "- 正在前往走廊（还有 8 步）",
        "- [14:00:00] 阿美在休息区说：「你好。」",
    ]:
        assert fragment in text, fragment
    assert text.index("【时间】") < text.index("【位置】") < text.index("【在场的人】") \
        < text.index("【可交互的东西】") < text.index("【你的状态】") < text.index("【新的感知】")

    guest_view = render(w, "guest", [])
    assert "- 我（住客）：正在走动" in guest_view
    assert "正在说「你好。」，已说出「你好。」" in guest_view
    assert guest_view.endswith("【新的感知】无")


def test_render_job_progress_and_hidden_actions(h):
    w = h.world
    h.place("worker", 11, 6)
    h.place("player", 10, 5)
    w.interact("worker", "保险柜", "crack")
    h.advance(4)
    worker_view = render(w, "worker", [])
    assert "- 正在撬保险柜（4/20 秒）" in worker_view
    assert "· 可以撬保险柜（行动 crack，约需 20 秒）" in worker_view
    w.stop("worker")
    assert "约需 20 秒，已完成 4 秒" in render(w, "worker", [])

    player_view = render(w, "player", h.drain("player"))
    assert "- 老李（工人）" in player_view
    assert "撬保险柜（行动" not in player_view        # 玩家不在 who 中，不列出该行动
    assert "传来金属刮擦声" in player_view


def test_render_offstage_and_door(h):
    w = h.world
    h.place("player", 9, 1)
    assert "【位置】你在休息区与走廊之间的门口。" in render(w, "player", [])
    assert "阿美（住客）" in render(w, "player", [])
    assert "除你之外没有别人" in render(w, "worker", [])
    assert "【位置】你在门外。" in render(w, "worker", [])
