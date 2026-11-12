"""第四轮只读审查：UI / CLI / 入口层回归用例。

只新建这一个测试文件，不改任何既有 tests/ 文件。tkinter 替身与路径改道
复用 tests/_ui_support.py 的 prepare()（必须在 import hsai.ui 之前调用）。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from tests._ui_support import prepare, widget_texts

prepare()                       # 没有 tkinter 就装替身 + 改道状态文件

import hsai.ui as ui_mod                        # noqa: E402
import hsai.ui_ai as ui_ai_mod                  # noqa: E402
import hsai.ui_state as ui_state_mod            # noqa: E402
from hsai.ui import Overlay                     # noqa: E402
from hsai.ui_theme import DEFAULT_WIDTH, LAYOUT_VERSION   # noqa: E402
from hsai import ai as ai_mod                   # noqa: E402
from hsai import cards, memory, feedback       # noqa: E402
from hsai import cli_boot, cli_deck, cli_live, cli_meta, cli_review   # noqa: E402
from hsai import cli as cli_mod                  # noqa: E402
from hsai import review as review_mod           # noqa: E402
from hsai.state import GameState                # noqa: E402


def _snap(**kw) -> dict:
    s = {"phase": "playing", "turn": 1, "actions": []}
    s.update(kw)
    return s


class _OverlayCase(unittest.TestCase):
    """共用：改道所有写盘路径、关掉 AI、装好替身 Overlay。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-r4-ui-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patches = [
            mock.patch.object(ui_mod, "STATE_PATH", self.root / "window.json"),
            mock.patch.object(ui_mod, "DATA_DIR", self.root),
            mock.patch.object(ui_mod, "LAST_REVIEW_JSON", self.root / "last_review.json"),
            mock.patch.object(ui_mod, "ADVICE_LOG_JSONL", self.root / "advice.jsonl"),
            mock.patch.object(ui_mod, "ADVICE_FEEDBACK_JSONL", self.root / "feedback.jsonl"),
            mock.patch.object(ui_mod.logutil, "log"),
            mock.patch.object(ai_mod, "is_ready", return_value=False),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [q.stop() for q in patches])

        self.db = cards.CardDB([])
        self.st = GameState(self.db)
        self.ov = Overlay(self.db, demo=True)
        self.ov._tick = lambda: None
        self.ov._render = lambda *a, **k: None
        self.ov._sync_bar = lambda *a, **k: None
        self.ov._render_collapsed = lambda *a, **k: None
        self.addCleanup(lambda: self.ov.quit())


# ① U1：跑过一拍后点区块标题，_toggle_section 必须用 _last_snap（dict），
#    不能再把 GameState 传给 ui_render._render（后者第一行就 snap.get）。
class SectionToggleUsesSnapTest(_OverlayCase):
    def test_toggle_section_after_a_frame_does_not_crash(self) -> None:
        ov = self.ov
        snap = _snap(turn=3)
        with mock.patch.object(ov.advisor, "snapshot", return_value=snap):
            ov._update(self.st)          # 这一拍把 _last_state 设成 GameState、_last_snap 设成 dict
        self.assertIsInstance(ov._last_state, GameState)
        self.assertIs(ov._last_snap, snap)

        # 复现 _render 的关键第一行：传进来的必须是 dict，否则 GameState 没有 .get
        def render_requires_dict(s):
            int(s.get("turn") or 0)

        before = set(ov._collapsed)
        with mock.patch.object(ov, "_render", side_effect=render_requires_dict) as render:
            ov._toggle_section("对战信息")
        render.assert_called_once_with(snap)
        self.assertNotEqual(set(ov._collapsed), before, "折叠状态确实要翻转")


# ② U3：坏 last_review.json 顶层非 dict 必须降级成 None，别污染渲染
class BadLastReviewTest(_OverlayCase):
    def test_non_dict_last_review_degrades_to_none(self) -> None:
        ov = self.ov
        path = ui_mod.LAST_REVIEW_JSON
        for content in ("[]", '"oops"', "null", "123"):
            path.write_text(content, encoding="utf-8")
            self.assertIsNone(ov._load_last_review(), f"{content} 应降级成 None")
        good = {"result": "win", "lessons": ["教训"]}
        path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(ov._load_last_review(), good)

    def test_render_last_review_survives_bad_data(self) -> None:
        ov = self.ov
        ui_mod.LAST_REVIEW_JSON.write_text("[1, 2]", encoding="utf-8")
        ov._last_review = ov._load_last_review()
        self.assertIsNone(ov._last_review)
        ov._render_last_review()         # 修复前 list 是真值，or {} 不降级 → AttributeError
        self.assertIn("上一局没有可复盘的记录", widget_texts(ov.body))


# ③ U7：GBK 控制台能编码中文时也要把 errors 放宽成 replace，否则 ⚠ 裸崩
class _FakeStream:
    def __init__(self, encoding: str, errors: str = "surrogateescape") -> None:
        self.encoding = encoding
        self.errors = errors
        self.calls: list[dict] = []

    def reconfigure(self, **kw) -> None:
        self.calls.append(kw)
        if "errors" in kw:
            self.errors = kw["errors"]


class ConsoleEncodingTest(unittest.TestCase):
    def test_gbk_console_widens_errors_without_changing_encoding(self) -> None:
        out, err = _FakeStream("gbk"), _FakeStream("gbk")
        with mock.patch.object(cli_boot, "sys", types.SimpleNamespace(stdout=out, stderr=err)):
            cli_boot._ensure_console()
        self.assertIn({"errors": "replace"}, out.calls)
        self.assertIn({"errors": "replace"}, err.calls)
        self.assertEqual(out.errors, "replace")
        self.assertFalse(any("encoding" in c for c in out.calls), "不能把 GBK 控制台改坏")

    def test_non_chinese_console_switches_to_utf8(self) -> None:
        out, err = _FakeStream("cp1252"), _FakeStream("cp1252")
        with mock.patch.object(cli_boot, "sys", types.SimpleNamespace(stdout=out, stderr=err)):
            cli_boot._ensure_console()
        self.assertIn({"encoding": "utf-8", "errors": "replace"}, out.calls)

    def test_stream_without_reconfigure_does_not_crash(self) -> None:
        plain = types.SimpleNamespace(encoding="gbk")
        fake_sys = types.SimpleNamespace(stdout=plain, stderr=plain)
        with mock.patch.object(cli_boot, "sys", fake_sys):
            cli_boot._ensure_console()   # AttributeError 必须被兜住


# ④ U2：deck mine --file 坏输入一律不覆盖我方卡组；合法输入才原子写盘
class _FakeCard:
    def __init__(self, cid: str) -> None:
        self.id = cid


class _FakeDB:
    def __init__(self, known: set[str]) -> None:
        self.known = known

    def by_name_or_id(self, key):
        return _FakeCard(key) if key in self.known else None


class DeckFileImportTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-r4-deck-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.deck_path = self.root / "our_deck.json"
        self.sentinel = '{"sentinel": true}'
        self.deck_path.write_text(self.sentinel, encoding="utf-8")
        patches = [
            mock.patch.object(cli_deck, "OUR_DECK_PATH", self.deck_path),
            mock.patch.object(cli_deck, "DATA_DIR", self.root),
            mock.patch.object(cli_deck, "_db", return_value=_FakeDB({"火球术", "寒冰箭"})),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [q.stop() for q in patches])

    def _run(self, path: Path) -> tuple[int, str]:
        args = argparse.Namespace(action="mine", code="", name="", file=str(path))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_deck.cmd_deck(args)
        return rc, buf.getvalue()

    def _assert_untouched(self) -> None:
        self.assertEqual(self.deck_path.read_text(encoding="utf-8"), self.sentinel)
        self.assertFalse(self.deck_path.with_suffix(".json.tmp").exists())

    def test_bad_json_does_not_overwrite(self) -> None:
        p = self.root / "bad.json"
        p.write_text("{oops", encoding="utf-8")
        rc, out = self._run(p)
        self.assertEqual(rc, 2)
        self.assertIn("读卡组文件失败", out)
        self._assert_untouched()

    def test_non_dict_or_bad_shape_does_not_overwrite(self) -> None:
        p = self.root / "shape.json"
        for payload in ("[]", "null", '"oops"', "{}", '{"cards": "abc"}',
                        '{"cards": []}', '{"cards": null}'):
            p.write_text(payload, encoding="utf-8")
            rc, _ = self._run(p)
            self.assertEqual(rc, 2, payload)
            self._assert_untouched()

    def test_missing_file_does_not_overwrite(self) -> None:
        rc, out = self._run(self.root / "nope.json")
        self.assertEqual(rc, 2)
        self.assertIn("读卡组文件失败", out)
        self._assert_untouched()

    def test_valid_dict_writes_atomically(self) -> None:
        p = self.root / "good.json"
        p.write_text(json.dumps({"name": "测试", "cards": ["火球术", "寒冰箭"]},
                                ensure_ascii=False), encoding="utf-8")
        rc, out = self._run(p)
        self.assertEqual(rc, 0)
        self.assertIn("已保存 2 张牌", out)
        data = json.loads(self.deck_path.read_text(encoding="utf-8"))
        self.assertEqual(data["cards"], ["火球术", "寒冰箭"])
        self.assertFalse(self.deck_path.with_suffix(".json.tmp").exists(), ".tmp 不能留")

    def test_deck_set_is_no_longer_an_action(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cli_mod.main(["deck", "set"])
        self.assertEqual(ctx.exception.code, 2)


# ⑤ U2：其它 CLI 参数族
class CliArgumentGuardTest(unittest.TestCase):
    def test_review_rejects_non_positive_n(self) -> None:
        for n in (0, -2):
            buf = io.StringIO()
            with mock.patch.object(review_mod, "load_games") as load:
                with contextlib.redirect_stdout(buf):
                    rc = cli_review.cmd_review(argparse.Namespace(n=n))
            self.assertEqual(rc, 2, f"n={n}")
            load.assert_not_called()          # 不能把全部历史刷出来
            self.assertNotIn("个我方回合", buf.getvalue())

    def test_meta_import_without_file_or_bad_json(self) -> None:
        with mock.patch.object(cli_meta, "_db", return_value=object()):
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = cli_meta.cmd_meta(argparse.Namespace(action="import", file=""))
            self.assertEqual(rc, 2)
            self.assertIn("用法", buf.getvalue())

            tmp = tempfile.TemporaryDirectory(prefix="hsai-r4-meta-")
            self.addCleanup(tmp.cleanup)
            p = Path(tmp.name) / "x.json"
            p.write_text("[bad", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as buf2:
                rc = cli_meta.cmd_meta(argparse.Namespace(action="import", file=str(p)))
            self.assertEqual(rc, 2)
            self.assertIn("读取卡组表失败", buf2.getvalue())

    def test_replay_missing_file(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-r4-replay-")
        self.addCleanup(tmp.cleanup)
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = cli_live.cmd_replay(argparse.Namespace(file=str(Path(tmp.name) / "nope.log")))
        self.assertEqual(rc, 2)
        self.assertIn("日志不存在", buf.getvalue()) if False else self.assertIn("不存在", buf.getvalue())


# ⑥ U4：AI 复盘异常要回滚标志；本地落盘失败不能连累其它落盘
class AiReviewRetryTest(_OverlayCase):
    def _thread_targets(self, fake) -> list:
        return [c[1].get("target") for c in fake.Thread.call_args_list]

    def test_failed_review_rolls_back_flag_and_can_retry(self) -> None:
        ov = self.ov
        ov._ai_reviewed = True
        with mock.patch.object(ai_mod, "review", side_effect=RuntimeError("offline")):
            ov._ai_review_worker(_snap(phase="game_over", turn=9))
        self.assertFalse(ov._ai_reviewed, "本局要允许重试")

        with mock.patch.object(ai_mod, "is_ready", return_value=True), \
                mock.patch.object(ui_ai_mod, "threading") as fake:
            ov._maybe_ai(_snap(phase="game_over", turn=9))
        self.assertIn(ov._ai_review_worker, self._thread_targets(fake))

    def test_local_write_failure_does_not_block_lessons(self) -> None:
        ov = self.ov
        with mock.patch.object(ai_mod, "review", return_value=(["教训"], [{"param": "tempo"}])), \
                mock.patch.object(memory, "add_tunings", side_effect=OSError("disk")), \
                mock.patch.object(memory, "add_lessons") as add_lessons:
            ov._ai_review_worker(_snap(phase="game_over", turn=9))
        add_lessons.assert_called_once()
        self.assertTrue(ov._ai_reviewed, "网络调用成功不该回滚标志")


# ⑦ U5：异步写回 _ai_lessons 后，等待界面要重画一次把教训显示出来
class AsyncLessonRedrawTest(_OverlayCase):
    def test_lessons_written_by_worker_show_up_in_review_panel(self) -> None:
        ov = self.ov
        ov._last_state = self.st
        ov._last_game_ts = str(getattr(self.st, "_create_ts", "") or "")
        ov._last_review = {"lessons": []}
        ov._review_deadline = time.time() + 30
        snap = _snap(phase="idle")

        with mock.patch.object(ov.advisor, "snapshot", return_value=snap), \
                mock.patch.object(ai_mod, "review", return_value=(["别在 6 费前铺满"], [])), \
                mock.patch.object(memory, "add_lessons"), \
                mock.patch.object(memory, "add_tunings"):
            ov._update(self.st)                  # 第一帧：画一次（还没有教训）
            self.assertTrue(ov._last_review_shown)
            self.assertNotIn("别在 6 费前铺满", widget_texts(ov.body))
            ov._ai_review_worker(snap)           # 模拟工作线程写回
            self.assertFalse(ov._last_review_shown, "新数据到了要允许重画一次")
            ov._update(self.st)                  # 第二帧：重画
        self.assertIn("别在 6 费前铺满", widget_texts(ov.body))

    def test_two_frames_without_new_data_still_render_once(self) -> None:
        """保住 tests/test_audit_fixes_round3.py:141-150「两帧只画一次」。"""
        ov = self.ov
        ov._last_state = self.st
        ov._last_game_ts = str(getattr(self.st, "_create_ts", "") or "")
        ov._last_review = {"lessons": ["上一局教训"]}
        ov._review_deadline = time.time() + 30
        snap = _snap(phase="idle")
        with mock.patch.object(ov, "_render_last_review") as render, \
                mock.patch.object(ov.advisor, "snapshot", return_value=snap):
            ov._update(self.st)
            ov._update(self.st)
        self.assertEqual(render.call_count, 1)


# ⑧ U6(a)：起反馈线程时传快照，worker 不读 self 的共享集合
class FeedbackSnapshotTest(_OverlayCase):
    def test_worker_uses_snapshot_even_if_shared_state_is_cleared(self) -> None:
        ov = self.ov
        ov._disagree_turns = {3}
        ov._advice_by_turn = {3: {"mana": 3, "advice": ["打脸"], "actions": ["火球术"]}}
        with mock.patch.object(ai_mod, "review", return_value=([], [])), \
                mock.patch.object(memory, "add_lessons"), \
                mock.patch.object(memory, "add_tunings"), \
                mock.patch.object(ui_ai_mod, "threading") as fake:
            ov._ai_review_worker(_snap(phase="game_over", turn=9))
        calls = [c for c in fake.Thread.call_args_list
                 if c[1].get("target") == ov._feedback_worker]
        self.assertEqual(len(calls), 1)
        turns, advice = calls[0][1]["args"]
        self.assertEqual(turns, [3])

        ov._disagree_turns = set()            # 模拟 new_game 跨局清零
        ov._advice_by_turn = {}
        with mock.patch.object(feedback, "build_summary", return_value="s") as bs, \
                mock.patch.object(feedback, "feedback", return_value=[]):
            ov._feedback_worker(turns, advice)
        self.assertEqual([c["turn"] for c in bs.call_args[0][0]], [3])


# ⑨ U6(b)：数据异常不该显示「渲染出错」，但必须留一行日志
class TickErrorSplitTest(_OverlayCase):
    def setUp(self) -> None:
        super().setUp()
        # 这两个用例要真的跑一遍 _tick，setUp 的防递归替身换回真方法
        self.ov._tick = type(self.ov)._tick.__get__(self.ov)

    def test_poll_failure_does_not_show_render_error(self) -> None:
        ov = self.ov
        patcher = mock.patch.object(ui_mod.logutil, "log")
        log = patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(ov.manager, "poll", side_effect=OSError("log gone")), \
                mock.patch.object(ov, "_show_render_error") as show_err:
            for _ in range(3):
                ov._tick()
        show_err.assert_not_called()
        self.assertNotEqual(ov.bar_label.cget("text"), "渲染出错，按 F8 重载")
        self.assertTrue(any("data error" in str(c.args[0]) for c in log.mock_calls if c.args))

    def test_update_failure_is_logged(self) -> None:
        ov = self.ov
        patcher = mock.patch.object(ui_mod.logutil, "log")
        log = patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(ov.manager, "poll", return_value=(self.st, True)), \
                mock.patch.object(ov, "_update", side_effect=RuntimeError("bad render")):
            ov._tick()
        self.assertTrue(any("tick error" in str(c.args[0]) for c in log.mock_calls if c.args))


# ⑩ U8：window.json 坏数值启动钳到交互路径同一组边界
class WindowRangeTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-r4-window-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "window.json"
        patches = [
            mock.patch.object(ui_mod, "STATE_PATH", self.path),
            mock.patch.object(ui_mod, "DATA_DIR", self.root),
            mock.patch.object(ui_mod, "ADVICE_LOG_JSONL", self.root / "advice.jsonl"),
            mock.patch.object(ui_mod.logutil, "log"),
            mock.patch.object(ai_mod, "is_ready", return_value=False),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [q.stop() for q in patches])

    def _overlay(self, raw: str):
        self.path.write_text(raw, encoding="utf-8")
        ov = Overlay(cards.CardDB([]), demo=True)
        ov._tick = lambda: None
        self.addCleanup(lambda: ov.quit())
        return ov

    def test_nan_alpha_falls_back_to_default(self) -> None:
        ov = self._overlay(json.dumps({"layout": LAYOUT_VERSION, "alpha": float("nan")}))
        self.assertEqual(ov.alpha, 0.90)

    def test_alpha_clamped(self) -> None:
        ov = self._overlay(json.dumps({"layout": LAYOUT_VERSION, "alpha": 999.0}))
        self.assertEqual(ov.alpha, 1.0)

    def test_font_and_width_clamped(self) -> None:
        ov = self._overlay(json.dumps({"layout": LAYOUT_VERSION,
                                       "font_size": -50, "w": -100}))
        self.assertEqual(ov.font_size, 8)
        self.assertGreaterEqual(ov.width, 120)

    def test_empty_state_uses_defaults(self) -> None:
        ov = self._overlay("{}")
        self.assertEqual(ov.alpha, 0.90)
        self.assertEqual(ov.font_size, 10)
        self.assertEqual(ov.width, DEFAULT_WIDTH)

    def test_helpers_are_backward_compatible(self) -> None:
        self.assertEqual(ui_state_mod._as_int("x", 10), 10)
        self.assertEqual(ui_state_mod._as_float("0.5", 0.9), 0.5)
        self.assertEqual(ui_state_mod._as_float(float("inf"), 0.9), 0.9)
        self.assertEqual(ui_state_mod._as_int(99, 10, 8, 14), 14)


# ⑪ U9：_save_state / advice_log 截断都走原子写；写失败要留日志
class AtomicWriteTest(unittest.TestCase):
    def test_save_state_atomic_and_logs_failure(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-r4-save-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "window.json"
        with mock.patch.object(ui_mod, "STATE_PATH", path), \
                mock.patch.object(ui_mod, "DATA_DIR", Path(tmp.name)), \
                mock.patch.object(ui_mod.logutil, "log"):
            ui_state_mod._save_state({"x": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"x": 1})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

        fake_os = types.SimpleNamespace(replace=mock.Mock(side_effect=OSError("denied")))
        with mock.patch.object(ui_state_mod, "os", fake_os), \
                mock.patch.object(ui_mod, "STATE_PATH", path), \
                mock.patch.object(ui_mod, "DATA_DIR", Path(tmp.name)), \
                mock.patch.object(ui_mod.logutil, "log") as log:
            ui_state_mod._save_state({"x": 2})
        self.assertTrue(log.called, "写失败不能静默")

    def test_advice_log_truncation_is_atomic(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="hsai-r4-advice-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "advice.jsonl"
        path.write_text("\n".join(json.dumps({"turn": i}) for i in range(400)) + "\n",
                        encoding="utf-8")
        patches = [
            mock.patch.object(ui_mod, "STATE_PATH", Path(tmp.name) / "window.json"),
            mock.patch.object(ui_mod, "DATA_DIR", Path(tmp.name)),
            mock.patch.object(ui_mod, "ADVICE_LOG_JSONL", path),
            mock.patch.object(ui_mod.logutil, "log"),
            mock.patch.object(ai_mod, "is_ready", return_value=False),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [q.stop() for q in patches])
        ov = Overlay(cards.CardDB([]), demo=True)
        ov._tick = lambda: None
        self.addCleanup(lambda: ov.quit())
        snap = _snap(turn=401, advice={"lines": [{"title": "打脸", "reason": "能斩"}]})
        ov._log_advice(snap)
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 400)
        self.assertFalse(path.with_suffix(".jsonl.tmp").exists())


# ⑫ U10：坏数据别把字面 None / "?" 摆上面板
class BadRenderValueTest(_OverlayCase):
    def test_missing_candidate_name_shows_unknown(self) -> None:
        ov = self.ov
        ov._render_prediction({"name": "快攻战", "confidence": 0.2,
                               "candidates": [{"name": "快攻战", "confidence": 0.2},
                                              {"name": None, "confidence": 0.1}]})
        texts = " ".join(widget_texts(ov.body))
        self.assertIn("未知", texts)
        self.assertNotIn("None", texts)

    def test_missing_likely_hand_name_shows_unknown(self) -> None:
        ov = self.ov
        ov._render_prediction({"name": "未知卡组", "confidence": 0.0,
                               "likely_hand": [{"name": None, "cost": 3}]})
        texts = " ".join(widget_texts(ov.body))
        self.assertIn("未知", texts)
        self.assertNotIn("None", texts)


if __name__ == "__main__":
    unittest.main()
