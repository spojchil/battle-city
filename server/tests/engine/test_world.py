import pytest
import yaml
from conftest import MINI

from linx_game.engine import World, load_scenario, parse_scenario
from linx_game.engine.grid import chebyshev


def pos(h, cid):
    return h.world.characters[cid].pos


# ---- 移动 ----

def test_walk_speed_and_arrival(h):
    w = h.world
    assert w.move_to("player", (6, 2)) == "你开始走向休息区，约 2 秒到达"
    h.advance(1.0)
    assert pos(h, "player") == (4, 2)
    h.advance(0.5)
    assert pos(h, "player") == (5, 2)
    h.advance(0.7)
    assert pos(h, "player") == (6, 2)
    assert not w.characters["player"].path
    assert [p.text for p in h.drain("player", "arrived")] == ["你到达了休息区"]
    assert [e.data["pos"] for e in h.events_of("moved")] == [[4, 2], [5, 2], [6, 2]]


def test_move_to_targets(h):
    w = h.world
    assert w.move_to("player", "厨房") == "找不到名为「厨房」的地点"
    assert w.move_to("player", "台灯") == "你已经在台灯旁边"
    assert w.move_to("player", "阿美").startswith("你开始走向阿美身边")
    h.advance(10)
    assert chebyshev(pos(h, "player"), pos(h, "guest")) == 1
    assert w.move_to("player", "储藏间").startswith("你开始走向储藏间")
    h.advance(10)
    assert w.grid.room_at(pos(h, "player")) == "store"


def test_step_rate_limit(h):
    w = h.world
    assert w.step("player", 1, 0)
    assert not w.step("player", 1, 0)          # 未到一步的时间
    h.advance(0.7)
    assert w.step("player", 1, 0)
    assert pos(h, "player") == (5, 2)
    assert not w.step("player", 1, 1)          # 只能四邻接单格
    h.place("player", 2, 3)
    h.advance(0.7)
    assert not w.step("player", 0, 1)          # 墙


def test_step_cancels_path(h):
    w = h.world
    w.move_to("player", "走廊")
    assert w.step("player", 0, 1)
    assert not w.characters["player"].path


def test_allowed_door(h):
    w = h.world
    assert w.move_to("guard", "休息区") == "无法到达休息区"
    h.place("player", 5, 3)
    assert not w.step("player", 0, 1)
    assert w.move_to("worker", "办公室").startswith("你开始走向办公室")
    h.advance(15)
    assert w.grid.room_at(pos(h, "worker")) == "office"


# ---- 场外 ----

def test_offstage_room(h):
    w = h.world
    h.place("player", 2, 2)
    h.place("worker", 0, 2)
    assert w.move_to("player", "门外") == "无法到达门外"
    assert w.move_to("player", "老李") == "找不到名为「老李」的地点"
    assert w.visible_characters("player") == []
    assert next(c for c in w.snapshot()["characters"] if c["id"] == "worker")["offstage"]

    w.say("worker", "有人吗？", "大喊")
    w.say("player", "外面有人吗？", "大喊")
    h.advance(3)
    assert h.drain("player") == []
    assert h.drain("worker") == []

    w.move_to("worker", "休息区")
    h.advance(1.8)                             # 踏上门格进入视野，确认期过后报告
    entered = h.drain("player", "enter")
    assert [p.text for p in entered] == ["老李（工人）来到了休息区"]
    assert entered[0].wake


# ---- 进出与脚步声 ----

def test_enter_leave_footsteps(h):
    w = h.world
    h.place("player", 6, 1)
    h.place("guest", 10, 2)
    w.move_to("player", (8, 1))
    h.advance(1.5)
    steps = h.drain("guest")
    assert [p.kind for p in steps] == ["footsteps"]
    assert "休息区" in steps[0].text and "脚步声" in steps[0].text and steps[0].wake

    # 10 秒内同一对角色只报一次
    w.move_to("player", (7, 1))
    h.advance(0.7)
    w.move_to("player", (8, 1))
    h.advance(0.7)
    assert h.drain("guest", "footsteps") == []
    w.move_to("player", (7, 1))
    h.advance(10)
    w.move_to("player", (8, 1))
    h.advance(0.7)
    assert len(h.drain("guest", "footsteps")) == 1

    h.advance(0.7)
    assert w.step("player", 1, 0)              # 踏上门格
    h.advance(1.1)
    assert [p.text for p in h.drain("guest")] == ["我（住客）来到了走廊"]
    assert w.step("player", -1, 0)
    h.advance(1.1)
    assert [p.text for p in h.drain("guest")] == ["我（住客）离开了走廊"]


def test_brief_visibility_change_cancels_out(h):
    w = h.world
    h.place("player", 8, 1)
    h.place("guest", 10, 2)
    assert w.step("player", 1, 0)              # 踏上门格，随即退回
    h.advance(0.7)
    assert w.step("player", -1, 0)
    h.advance(2)
    assert [p.kind for p in h.drain("guest") if p.kind in ("enter", "leave")] == []


def test_audibility(h):
    w = h.world
    h.place("guest", 10, 1)
    h.place("player", 7, 1)                    # 隔一扇门，声音代价 6
    assert w.audibility("guest", "player") == ("clear", round(1 - 0.5 * 6 / 7, 3))
    w.say("guest", "这是一句悄悄话，说得很慢很慢。", "耳语")
    assert w.audibility("guest", "player") == (None, 0.0)    # 取正在说的话的音量
    assert w.audibility("guest", "player", "大喊")[0] == "clear"
    h.place("worker", 0, 2)
    h.place("player", 2, 2)
    assert w.audibility("worker", "player", "大喊") == (None, 0.0)
    assert w.audibility("player", "worker", "大喊") == (None, 0.0)


def test_move_to_blocked_coordinate(h):
    w = h.world
    h.place("player", 6, 2)
    assert w.move_to("player", (2, 1)).startswith("你开始走向台灯旁边")
    h.advance(3)
    assert pos(h, "player") == (3, 2)          # 可交互点旁最近的可站立格
    assert w.move_to("player", (0, 0)) == "无法到达(0, 0)附近"


# ---- 说话 ----

def test_speech_sentences_and_abort(h):
    w = h.world
    h.place("guest", 5, 2)
    sp = w.say("guest", "第一句。第二句话！第三句。")
    assert sp.sentences == ["第一句。", "第二句话！", "第三句。"]
    heard = h.drain("player", "speech")
    assert [p.text for p in heard] == ["阿美在休息区说：「第一句。」"]
    assert heard[0].wake and heard[0].data["level"] == "clear"

    h.advance(1.0)                             # 4 字 × 0.22 秒后开始第二句
    assert [p.data["sentence"] for p in h.drain("player", "speech")] == ["第二句话！"]
    assert w.snapshot()["characters"][3]["speaking"]

    assert w.stop_speaking("guest") == "你停了下来，已说出「第一句。第二句话！」"
    assert sp.done and sp.aborted
    h.advance(3)
    assert h.drain("player") == []
    end = h.events_of("speech_end")[-1].data
    assert end == {"cid": "guest", "speech_id": sp.id, "aborted": True, "spoken": "第一句。第二句话！"}
    assert h.events_of("speech")[0].data["hearers"]["player"]["level"] == "clear"
    assert [p.data["sentence"] for p in w.recordings["player"]] == ["第一句。", "第二句话！"]


def test_speech_finishes_naturally(h):
    w = h.world
    sp = w.say("guest", "好。")
    h.advance(0.5)
    assert sp.done and not sp.aborted
    assert w.characters["guest"].speech is None
    assert w.stop_speaking("guest") == "你没有在说话"


def test_new_speech_aborts_old(h):
    w = h.world
    first = w.say("guest", "这是一句很长很长的话。后面还有。")
    second = w.say("guest", "算了。")
    assert first.aborted and not second.done
    assert first.spoken == ["这是一句很长很长的话。"]


@pytest.mark.parametrize("guest_at, player_at, volume, kind, text", [
    ((6, 2), (3, 2), "耳语", "muffled", "阿美在东边低声说话，听不清内容"),
    ((10, 1), (7, 1), "正常", "speech", "阿美在走廊说：「喂。」"),
    ((10, 1), (5, 1), "正常", "muffled", "东边（走廊）有人在说话，听不清内容"),
    ((11, 2), (3, 2), "大喊", "speech", "阿美在走廊大声喊：「喂。」"),
    ((11, 2), (3, 2), "正常", None, None),
])
def test_speech_levels(h, guest_at, player_at, volume, kind, text):
    h.place("guest", *guest_at)
    h.place("player", *player_at)
    h.world.say("guest", "喂。", volume)
    got = h.drain("player")
    if kind is None:
        assert got == []
    else:
        assert [(p.kind, p.text) for p in got] == [(kind, text)]
        if kind == "muffled":
            assert not got[0].wake
            assert "喂" not in got[0].text


def test_voice_backend(h):
    class FakeVoice:
        def __init__(self):
            self.started, self.aborted = [], []

        def start(self, speech):
            self.started.append(speech)

        def abort(self, speech):
            self.aborted.append(speech)

    w = h.world
    w.voice = voice = FakeVoice()
    h.place("guest", 5, 2)
    sp = w.say("guest", "一。二。")
    assert voice.started == [sp]
    h.advance(2)
    assert h.drain("player") == []            # 后端未报告前没有感知
    w.speech_sentence("guest", sp.id, "一。")
    assert [p.data["sentence"] for p in h.drain("player")] == ["一。"]

    sp2 = w.say("guest", "三。")
    assert voice.aborted == [sp] and sp.aborted
    w.speech_sentence("guest", sp.id, "二。")  # 已中止的话，回调被忽略
    assert h.drain("player") == []
    w.speech_finished("guest", sp2.id, aborted=False)
    assert sp2.done and w.characters["guest"].speech is None


def test_player_said(h):
    w = h.world
    h.place("guest", 5, 2)
    w.player_said("player", "你好。请问几点了？")
    assert [p.data["sentence"] for p in h.drain("guest", "speech")] == ["你好。", "请问几点了？"]
    assert [p.text for p in w.recordings["player"]] == ["你说：「你好。」", "你说：「请问几点了？」"]


# ---- 交互 ----

def test_interact_completion_effects(h):
    w = h.world
    assert w.interact("player", "电视", "打开") == "找不到名为「电视」的可交互物"
    assert w.interact("player", "台灯", "砸").startswith("台灯没有「砸」这个行动，现在可以：打开台灯。")
    assert w.interact("player", "lamp", "switch_on") == "你开始打开台灯，还需约 2 秒"
    assert w.snapshot()["characters"][0]["activity"] == "在开台灯"
    h.advance(2.1)

    assert w.flags["lamp"] == "on"
    assert w.describe(w.scenario.interactables[0]) == ["台灯亮着"]
    done = h.drain("player")
    assert [(p.kind, p.text) for p in done if p.kind in ("job_done", "reveal")] == [
        ("job_done", "你完成了打开台灯"), ("reveal", "灯亮了，照出桌上的字条。")]
    assert [p.text for p in h.drain("guard", "notify")] == ["监控显示休息区的灯亮了。"]
    assert [e.data for e in h.events_of("flag")] == [{"key": "lamp", "value": "on", "old": "off"}]
    assert h.events_of("job_end")[-1].data["completed"]
    assert w.interact("player", "台灯", "打开台灯") == "现在无法打开台灯"


def test_interact_walks_there_first(h):
    w = h.world
    h.place("player", 6, 2)
    assert w.interact("player", "台灯", "打开台灯") == "你走向台灯，到达后开始打开台灯"
    assert w.characters["player"].job is None
    h.advance(2.2)                             # 3 步
    assert pos(h, "player") == (3, 2)
    assert [p.text for p in h.drain("player", "arrived")] == ["你到达了台灯旁边，你开始打开台灯，还需约 2 秒"]
    assert w.characters["player"].job is not None
    h.advance(2.1)
    assert w.flags["lamp"] == "on"
    assert w.interact("guard", "台灯", "打开台灯") == "现在无法打开台灯"


@pytest.mark.parametrize("cancel", [
    lambda w: w.stop("player"),
    lambda w: w.move_to("player", "走廊"),
    lambda w: w.step("player", 0, 1),
])
def test_interact_walk_cancelled(h, cancel):
    w = h.world
    h.place("player", 6, 2)
    w.interact("player", "台灯", "打开台灯")
    h.advance(0.5)                             # 尚未迈出第一步，step 不受限速影响
    cancel(w)
    h.advance(6)
    assert w.characters["player"].job is None
    assert w.characters["player"].pending is None
    assert w.flags["lamp"] == "off"


def test_interact_unreachable(h):
    assert h.world.interact("guard", "台灯", "打开台灯") == "无法走到台灯旁边"


def test_interrupt_restarts_non_cumulative(h):
    w = h.world
    w.interact("player", "台灯", "打开台灯")
    h.advance(1)
    assert w.step("player", 1, 0)
    assert w.characters["player"].job is None
    assert not h.events_of("job_end")[-1].data["completed"]
    h.advance(0.7)
    assert w.step("player", -1, 0)
    assert w.interact("player", "台灯", "打开台灯") == "你开始打开台灯，还需约 2 秒"


def test_cumulative_progress_and_sound(h):
    w = h.world
    h.place("worker", 11, 6)
    h.place("player", 10, 5)
    assert w.interact("player", "保险柜", "撬保险柜") == "你不能撬保险柜"
    assert w.interact("worker", "保险柜", "crack") == "你开始撬保险柜，还需约 20 秒"
    h.advance(8)
    sounds = h.drain("player", "sound")
    assert len(sounds) == 2                    # 开始时一次，第 5 秒一次
    assert sounds[0].text == "东南边传来金属刮擦声" and not sounds[0].wake
    assert len(w.recordings["player"]) == 2
    assert len(h.events_of("sound")) == 2

    assert w.stop("worker") == "你停止了撬保险柜（已进行 8/20 秒，进度会保留）"
    assert w.progress[("safe", "crack")] == pytest.approx(8)
    assert w.interact("worker", "保险柜", "crack") == "你开始撬保险柜（接着之前的进度），还需约 12 秒"
    h.advance(11.5)
    assert "safe" not in w.flags
    h.advance(1)
    assert w.flags["safe"] == "open"
    assert ("safe", "crack") not in w.progress
    assert [p.text for p in h.drain("worker", "reveal")] == ["柜门开了。"]


def test_move_interrupts_job(h):
    w = h.world
    h.place("worker", 11, 6)
    w.interact("worker", "保险柜", "crack")
    h.advance(3)
    msg = w.move_to("worker", "走廊")
    assert msg.startswith("你停止了撬保险柜（已进行 3/20 秒，进度会保留）。你开始走向走廊")
    assert w.progress[("safe", "crack")] == pytest.approx(3)


# ---- 拍照与快照 ----

def test_photo(h):
    w = h.world
    h.place("player", 10, 5)
    h.place("worker", 11, 6)
    w.interact("worker", "保险柜", "crack")
    w.say("worker", "别出声。")
    scene = w.photo("player")
    assert scene["index"] == 0 and scene["place"] == "储藏间" and scene["clock"] == "14:00:00"
    assert scene["people"] == [{"id": "worker", "name": "老李", "role": "工人",
                                "activity": "在撬保险柜", "speaking": True, "through_door": False}]
    assert "老李（工人）：在撬保险柜，正在说话" in scene["text"]
    h.advance(21)
    scene = w.photo("player")
    assert scene["index"] == 1 and "保险柜：柜门敞开着" in scene["text"]
    assert len(w.photos["player"]) == 2


def test_speaking_nearby(h):
    w = h.world
    h.place("player", 3, 2)
    h.place("guest", 4, 2)
    h.place("guard", 3, 6)                     # 隔墙的办公室
    assert w.speaking_nearby("player") == []
    w.say("guest", "这句话要说得久一点才行。")
    w.say("guard", "办公室里也有人在说话。")
    assert [c.id for c in w.speaking_nearby("player")] == ["guest"]
    h.advance(30)
    assert w.speaking_nearby("player") == []
    w.live_speaking.add("player")              # 玩家按住麦克风说话
    assert [c.id for c in w.speaking_nearby("guest")] == ["player"]


def test_snapshot_for_viewer(h):
    w = h.world
    h.place("player", 5, 3)                    # 紧挨通往办公室的门 (5, 4)
    view = lambda: w.snapshot("player")
    ids = {c["id"] for c in view()["characters"]}
    assert "player" in ids and "guard" not in ids
    assert "flags" not in view()
    assert "drawer" not in {it["id"] for it in view()["interactables"]}

    w.flags["office_door"] = "ajar"
    assert "guard" in {c["id"] for c in view()["characters"]}
    assert "drawer" in {it["id"] for it in view()["interactables"]}


def test_open_plan_rooms_see_each_other():
    data = yaml.safe_load(MINI.read_text())
    rows = data["map"]["grid"].splitlines()
    rows[2] = rows[2][:9] + "a" + rows[2][10:]          # 打通休息区与走廊之间的墙
    data["map"]["grid"] = "\n".join(rows) + "\n"
    w = World(parse_scenario(data))
    assert w.grid.areas["lounge"] == {"lounge", "hall"}
    assert w.grid.areas["office"] == {"office"}
    w.characters["player"].pos, w.characters["guest"].pos = (2, 1), (11, 3)
    assert "guest" in {o.id for o in w.visible_characters("player")}
    assert World(load_scenario(MINI)).grid.areas["lounge"] == {"lounge"}


def test_snapshot_and_events(h):
    w = h.world
    w.say("guest", "嗯。")
    w.interact("player", "台灯", "打开台灯")
    w.move_to("worker", "休息区")
    h.advance(3)
    snap = w.snapshot()
    assert snap["clock"] == "14:00:03" and snap["remaining_s"] == pytest.approx(597)
    assert {c["id"] for c in snap["characters"]} == {"player", "worker", "guard", "guest"}
    assert {"lamp": "on"}.items() <= snap["flags"].items()
    kinds = {e.kind for e in h.events}
    assert {"moved", "speech", "speech_end", "job_start", "job_end", "flag", "percept"} <= kinds
    percept_event = next(e for e in h.events if e.kind == "percept")
    assert set(percept_event.data) == {"cid", "percept"}
