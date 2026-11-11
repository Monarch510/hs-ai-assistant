"""审计修复 UI 回归用例（第三轮复查 + A 组复盘生命周期）。

两个来源文件都用 tests/_ui_support.py 的 tkinter 替身，prepare() 必须在
import hsai.ui 之前。_snap 有两份语义不同的构造（空手牌 / 带火球术），
分别叫 _snap_r3 / _snap；两个 Overlay 基类 setUp 略有差异，各自保留。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hsai import cards, state

from tests._ui_support import prepare

prepare()                       # 装 tkinter 替身 + 改道 window.json 等状态文件

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
import hsai.ui_ai as ui_ai_mod  # noqa: E402
import hsai.ui_state as ui_state_mod  # noqa: E402
from hsai import ai as ai_mod   # noqa: E402
from hsai import ui_text        # noqa: E402
from hsai.advisor import board_from_state  # noqa: E402
from hsai.policy_actions import apply_action, legal_actions  # noqa: E402
from hsai.policy_lethal import max_face_damage  # noqa: E402
from hsai.powerlog import tag_key  # noqa: E402
from hsai.state import ZONE_PLAY, Entity, GameState  # noqa: E402
from hsai.ui import Overlay     # noqa: E402
from hsai.ui_state import _as_float, _as_int, _as_set  # noqa: E402


DB = cards.load()


def _snap_r3(**kw) -> dict:
    snap = {
        "phase": "playing", "turn": 3, "turn_known": True, "is_our_turn": True,
        "actions": [],
        "advice": {"lines": [{"title": "先打脸", "reason": "能斩"}],
                   "lethal": False, "defense": []},
        "our": {"mana": 5, "hand": [], "board": []},
        "opp": {"board": []},
        "end_turn_seq": 0, "turn_ending": False,
    }
    snap.update(kw)
    return snap


def _state_with_weapon(durability: int = 3) -> GameState:
    """和 tests/test_advisor_board.py 同一套构造：英雄 ATK=3 + 3 攻武器。"""
    st = GameState(DB)
    st.turn = 3
    st.turn_owner = 1
    st.our_id, st.opp_id = 1, 2
    st.player_ids = {1: 10}
    st.entities[10] = Entity(10, tags={"PLAYER_ID": 1, "HERO_ENTITY": 20})
    st.entities[20] = Entity(20, card_id="HERO_01", controller=1, zone=ZONE_PLAY,
                             tags={"ATK": 3, "HEALTH": 30})
    st.entities[30] = Entity(30, card_id="CS2_106", controller=1, zone=ZONE_PLAY,
                             tags={"ATK": 3, "DURABILITY": durability})
    return st


class _OverlayCase(unittest.TestCase):
    """构造一个不开事件循环的 Overlay，渲染类方法替成空操作隔离被测逻辑。"""

    def setUp(self) -> None:
        log_patch = mock.patch.object(ui_mod.logutil, "log")
        log_patch.start()
        self.addCleanup(log_patch.stop)

        tmp = tempfile.TemporaryDirectory(prefix="hsai-r3-ui-")
        self.addCleanup(tmp.cleanup)
        adv_log = mock.patch.object(ui_mod, "ADVICE_LOG_JSONL",
                                    Path(tmp.name) / "advice_log.jsonl")
        adv_log.start()
        self.addCleanup(adv_log.stop)

        ready = mock.patch.object(ai_mod, "is_ready", return_value=False)
        ready.start()
        self.addCleanup(ready.stop)

        self.db = cards.CardDB([])
        self.st = GameState(self.db)
        self.ov = Overlay(self.db, demo=True)
        self.ov._tick = lambda: None
        self.ov._render = lambda *a, **k: None
        self.ov._sync_bar = lambda *a, **k: None
        self.ov._render_collapsed = lambda *a, **k: None
        self.addCleanup(self._close)

    def _close(self) -> None:
        try:
            self.ov.quit()
        except Exception:
            pass


# ① 攻击次数只放一个计数器：斩杀估算和 apply_action 必须同一口径
class AttackPoolConsistencyTest(unittest.TestCase):
    def _actual_swings(self, board) -> int:
        work = board.clone()
        swings = 0
        for _ in range(6):
            face = next((a for a in legal_actions(work, 0, DB)
                         if a.kind == "hero_attack" and a.target == "enemy_hero"), None)
            if face is None:
                break
            work = apply_action(work, 0, face, DB)
            swings += 1
        return swings

    def test_board_from_state_uses_single_attack_pool(self) -> None:
        b = board_from_state(_state_with_weapon())
        me = b.sides[0]
        self.assertEqual(me.weapon_attacks_left, 0,
                         "修复前武器池和英雄池各置 1，apply_action 会连打 2 次")
        self.assertEqual(me.hero_attacks_left, 1)

    def test_estimated_swings_match_actual_swings(self) -> None:
        b = board_from_state(_state_with_weapon())
        me = b.sides[0]
        est = min(2, max(0, me.hero_attacks_left))   # max_face_damage 只认 hero_attacks_left
        max_face_damage(b, 0, DB)                    # 估算路径不能抛
        self.assertEqual(self._actual_swings(b), est,
                         "估算次数必须等于实打次数，否则会漏判斩杀")


# ② 复盘面板不再每帧销毁重建
class ReviewRedrawTest(_OverlayCase):
    def test_idle_review_rendered_once_not_every_frame(self) -> None:
        ov = self.ov
        # 预置成「非新局」，否则首帧 new_game 会把注入的展示窗口清掉
        ov._last_state = self.st
        ov._last_game_ts = str(getattr(self.st, "_create_ts", "") or "")
        ov._last_review = {"lessons": ["上一局教训"]}
        ov._review_deadline = time.time() + 30
        snap = _snap_r3(phase="idle")
        with mock.patch.object(ov, "_render_last_review") as render, \
                mock.patch.object(ov.advisor, "snapshot", return_value=snap):
            ov._update(self.st)
            ov._update(self.st)
        self.assertEqual(render.call_count, 1,
                         "修复前 _update 每帧把 _last_review_shown 清零，两帧各重画一次")
        self.assertTrue(ov._last_review_shown)


# ③ hand_count=None 显示「未知」，别把 None 摆上面板
class HandCountUnknownTest(unittest.TestCase):
    def test_none_shows_unknown(self) -> None:
        side = {"health": 30, "hand_count": None, "deck_count": 22, "board": []}
        row = ui_text.hud_row(side, "对手")
        self.assertIn("手牌 未知", row)
        self.assertNotIn("None", row)

    def test_value_still_shown(self) -> None:
        side = {"health": 30, "hand_count": 5, "deck_count": 22, "board": []}
        self.assertIn("手牌 5", ui_text.hud_row(side, "对手"))


# ④ window.json 坏值降级，不让整窗起不来
class WindowStateGuardTest(unittest.TestCase):
    def test_helpers_degrade_bad_values(self) -> None:
        self.assertEqual(_as_int("x", 10), 10)
        self.assertEqual(_as_int(None, 10), 10)
        self.assertEqual(_as_int("7", 10), 7)
        self.assertEqual(_as_float("x", 0.9), 0.9)
        self.assertEqual(_as_float("0.5", 0.9), 0.5)
        self.assertEqual(_as_set("not-a-list"), set(), "字符串不能被 set() 拆成字符")
        self.assertEqual(_as_set(["a", "b"]), {"a", "b"})
        self.assertEqual(ui_state_mod._as_set(123), set())

    def test_bad_window_json_degrades_instead_of_crashing(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-r3-window-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "window.json"
        bad = {"layout": "oops", "alpha": "oops", "font_size": {},
               "x": "oops", "y": None, "w": [], "collapsed_sections": "oops"}
        path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")

        patches = [
            mock.patch.object(ui_mod.logutil, "log"),
            mock.patch.object(ui_mod, "ADVICE_LOG_JSONL", Path(tmp.name) / "advice.jsonl"),
            mock.patch.object(ui_mod, "STATE_PATH", path),
            mock.patch.object(ai_mod, "is_ready", return_value=False),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [q.stop() for q in patches])

        ov = Overlay(cards.CardDB([]), demo=True)
        ov._tick = lambda: None
        ov._render = lambda *a, **k: None
        ov._sync_bar = lambda *a, **k: None
        ov._render_collapsed = lambda *a, **k: None
        self.addCleanup(lambda: ov.quit())

        self.assertEqual(ov.alpha, 0.90, "layout 坏 → 不认旧布局，alpha 用默认")
        self.assertEqual(ov.font_size, 10, "{} 降级成默认字号")
        self.assertEqual(ov._collapsed, set(), "collapsed_sections 是字符串时降级成空集合")


# ⑤ 跨局重置不再只挂 mulligan
class NewGameResetTest(_OverlayCase):
    def test_new_state_resets_even_without_mulligan(self) -> None:
        ov = self.ov
        ov._ai_reviewed = True
        ov._advice_by_turn = {9: {"actions": ["上局动作"]}}
        ov._ai_turn = (9, True)
        ov._ai_result = ("stale",)
        # 修复前只有 phase=mulligan 且 turn<=1 才重置，playing 半途接入会漏
        snap = _snap_r3(phase="playing", turn=9)
        with mock.patch.object(ov.advisor, "snapshot", return_value=snap):
            ov._update(self.st)
        self.assertFalse(ov._ai_reviewed)
        self.assertEqual(ov._advice_by_turn, {})
        self.assertIsNone(ov._ai_turn)
        self.assertIsNone(ov._ai_result)

    def test_same_state_does_not_re_reset(self) -> None:
        ov = self.ov
        snap = _snap_r3(phase="playing", turn=9)
        with mock.patch.object(ov.advisor, "snapshot", return_value=snap):
            ov._update(self.st)          # 第一次：新对象，重置
        ov._advice_by_turn = {9: {"actions": ["本局动作"]}}
        with mock.patch.object(ov.advisor, "snapshot", return_value=snap):
            ov._update(self.st)          # 第二次：同一局，不能重复重置
        self.assertEqual(ov._advice_by_turn, {9: {"actions": ["本局动作"]}})




# ⑧ 数字 tag 也过白名单，别让 tag=201 混进来
class TagWhitelistTest(unittest.TestCase):
    def test_digit_tag_outside_whitelist_is_dropped(self) -> None:
        self.assertIsNone(tag_key("201"), "TAG_NAMES 有 201 但不在 KNOWN_TAGS，数字形式也该丢")
        self.assertIsNone(tag_key("OVERRIDE_ENTITY_ID"))
        self.assertIsNone(tag_key("999999"))

    def test_known_digit_tags_still_work(self) -> None:
        self.assertEqual(tag_key("49"), "ZONE")
        self.assertEqual(tag_key("17"), "PLAYSTATE")
        self.assertEqual(tag_key("20"), "TURN")
        self.assertEqual(tag_key("90"), "NUM_ATTACKS_THIS_TURN")

    def test_name_form_uses_whitelist(self) -> None:
        self.assertEqual(tag_key("zone"), "ZONE")
        self.assertIsNone(tag_key("unknown_tag"))
        self.assertEqual(tag_key("STEP"), "STEP")




def _snap(**kw) -> dict:
    snap = {
        "phase": "playing", "turn": 3, "turn_known": True, "is_our_turn": True,
        "actions": [],
        "advice": {"lines": [{"title": "先打脸", "reason": "能斩"}],
                   "lethal": False, "defense": []},
        "our": {"mana": 5, "hand": [{"name": "火球术", "cost": 4}], "board": []},
        "opp": {"board": []},
        "end_turn_seq": 0, "turn_ending": False,
    }
    snap.update(kw)
    return snap


def _thread_targets(fake_threading) -> list:
    return [c[1].get("target") for c in fake_threading.Thread.call_args_list]


class _OverlayTestCase(unittest.TestCase):
    """构造一个不开事件循环的 Overlay，渲染类方法替成空操作隔离被测逻辑。"""

    def setUp(self) -> None:
        log_patch = mock.patch.object(ui_mod.logutil, "log")
        log_patch.start()
        self.addCleanup(log_patch.stop)

        tmp = tempfile.TemporaryDirectory(prefix="hsai-audit-ui-")
        self.addCleanup(tmp.cleanup)
        self._advice_log = Path(tmp.name) / "advice_log.jsonl"
        log_path = mock.patch.object(ui_mod, "ADVICE_LOG_JSONL", self._advice_log)
        log_path.start()
        self.addCleanup(log_path.stop)

        ready = mock.patch.object(ai_mod, "is_ready", return_value=False)
        ready.start()
        self.addCleanup(ready.stop)

        self.db = cards.CardDB([])
        self.st = state.GameState(self.db)
        self.ov = Overlay(self.db, demo=True)
        self.ov._tick = lambda: None
        self.ov._render = lambda *a, **k: None
        self.ov._sync_bar = lambda *a, **k: None
        self.ov._render_collapsed = lambda *a, **k: None
        self.addCleanup(self._close)

    def _close(self) -> None:
        try:
            self.ov.quit()
        except Exception:
            pass


class ReviewLifecycleTest(_OverlayTestCase):
    """缺陷 1 + G1：第二局 mulligan 必须把 _ai_reviewed 清零，复盘线程能再调度。"""

    def test_ai_reviewed_reset_and_worker_rescheduled_between_games(self) -> None:
        ov = self.ov
        # 第一局 game_over：复盘线程起一次，标志置 True
        with mock.patch.object(ai_mod, "is_ready", return_value=True), \
                mock.patch.object(ui_ai_mod, "threading") as fake_threading:
            ov._maybe_ai(_snap(phase="game_over", turn=9))
        self.assertTrue(ov._ai_reviewed, "第一局复盘后标志应为 True")
        self.assertIn(ov._ai_review_worker, _thread_targets(fake_threading),
                      "第一局应调度复盘线程")

        # 第二局回到 mulligan：_update 的重置块要把标志清零
        with mock.patch.object(ov.advisor, "snapshot",
                               return_value=_snap(phase="mulligan", turn=1)):
            ov._update(self.st)
        self.assertFalse(ov._ai_reviewed, "第二局 mulligan 应把 _ai_reviewed 清零")

        # 第二局 game_over：复盘线程必须能再次调度，否则 lessons/tunings 静默断更
        with mock.patch.object(ai_mod, "is_ready", return_value=True), \
                mock.patch.object(ui_ai_mod, "threading") as fake_threading2:
            ov._maybe_ai(_snap(phase="game_over", turn=9))
        self.assertIn(ov._ai_review_worker, _thread_targets(fake_threading2),
                      "第二局复盘线程应能再次调度")
        self.assertTrue(ov._ai_reviewed)


class LogAdviceTest(_OverlayTestCase):
    """缺陷 2：_log_advice 不能整桶覆盖掉 _update 先写进来的 actions。"""

    def test_keeps_actions_and_advice(self) -> None:
        ov = self.ov
        turn = 3
        ov._advice_by_turn[turn] = {"actions": ["火球术打脸"]}
        ov._log_advice(_snap(turn=turn))

        bucket = ov._advice_by_turn[turn]
        self.assertEqual(bucket.get("actions"), ["火球术打脸"],
                         "actions 不能被整桶覆盖吞掉")
        self.assertEqual(bucket.get("advice"), ["先打脸"])
        self.assertEqual(bucket.get("mana"), 5)

        rows = self._advice_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 1, "建议确实落盘（已改道到临时目录）")
        self.assertEqual(json.loads(rows[0])["turn"], turn)


class LoadStateGuardTest(unittest.TestCase):
    """缺陷 3：两份 _load_state 对顶层非 dict 的 window.json 都要兜底成 {}。"""

    def test_non_dict_top_level_returns_empty(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-audit-state-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "window.json"
        for content in ("null", "[]", '"x"', "123", "true"):
            path.write_text(content, encoding="utf-8")
            with mock.patch.object(ui_mod, "STATE_PATH", path):
                self.assertEqual(ui_mod._load_state(), {}, f"ui: {content}")
                self.assertEqual(ui_state_mod._load_state(), {}, f"ui_state: {content}")

    def test_valid_dict_still_returned(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-audit-state-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "window.json"
        data = {"layout": 7, "x": 1}
        path.write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.object(ui_mod, "STATE_PATH", path):
            self.assertEqual(ui_mod._load_state(), data)
            self.assertEqual(ui_state_mod._load_state(), data)


class HudRowDeckUnknownTest(unittest.TestCase):
    """G5：deck_count=None 显示「未知」，且别弄坏粗估标记。"""

    def test_deck_none_shows_unknown(self) -> None:
        side = {"health": 30, "hand_count": 3, "deck_count": None, "board": []}
        row = ui_text.hud_row(side, "对手")
        self.assertIn("牌库 未知", row)
        self.assertNotIn("None", row)


if __name__ == "__main__":
    unittest.main()
