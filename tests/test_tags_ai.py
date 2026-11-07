"""AI 补待定标签的离线测试：假 chat 返回，不发任何网络请求。"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from hsai import tags_ai


def _reply(rows: dict) -> str:
    return json.dumps({"rows": rows}, ensure_ascii=False)


class PendingLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.pending = self.dir / "card_tags_pending.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, lines: list[str]) -> None:
        self.pending.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_bad_lines_and_duplicates_are_skipped(self) -> None:
        self._write([
            json.dumps({"id": "CS2_029", "name": "火球术"}, ensure_ascii=False),
            "这不是 JSON",
            "",
            json.dumps({"name": "没有 id"}, ensure_ascii=False),
            json.dumps({"id": "CS2_029", "name": "重复"}, ensure_ascii=False),
        ])
        items, errors = tags_ai.load_pending(self.pending)
        self.assertEqual([item["id"] for item in items], ["CS2_029"])
        self.assertEqual(len(errors), 3)

    def test_missing_file_is_reported_not_raised(self) -> None:
        items, errors = tags_ai.load_pending(self.pending)
        self.assertEqual(items, [])
        self.assertIn("读不到", errors[0])

    def test_pending_stats_group_by_reason(self) -> None:
        items = [{"id": "a", "why": "奥秘"}, {"id": "b", "why": "抉择"},
                 {"id": "c", "why": "奥秘"}, {"id": "d"}]
        self.assertEqual(tags_ai.pending_stats(items),
                         {"奥秘": 2, "抉择": 1, "（未写原因）": 1})


class PromptTest(unittest.TestCase):
    def test_prompt_contains_tags_cards_and_plain_text(self) -> None:
        batch = [{"id": "CS2_029", "name": "火球术", "cost": 4, "type": "SPELL",
                  "text": "<b>造成</b>6点伤害。", "why": "规则拿不准"}]
        prompt = tags_ai.build_prompt(batch)
        for tag in ("单解", "斩杀", "护甲"):
            self.assertIn(tag, prompt)
        self.assertIn("CS2_029", prompt)
        self.assertIn("6点伤害", prompt)
        self.assertNotIn("<b>", prompt)
        self.assertIn("规则拿不准", prompt)


class ParseReplyTest(unittest.TestCase):
    def test_valid_rows_are_sorted_by_tag_order(self) -> None:
        text = _reply({
            "CS2_029": {"tags": ["斩杀", "单解", "单解"], "note": "解随从也能打脸"},
            "EX1_001": {"tags": []},
        })
        rows, errors = tags_ai.parse_reply(text, ["CS2_029", "EX1_001"])
        self.assertEqual(errors, [])
        self.assertEqual(rows["CS2_029"]["tags"], ["单解", "斩杀"])
        self.assertEqual(rows["EX1_001"]["tags"], [])
        self.assertIn("解随从", rows["CS2_029"]["note"])

    def test_fenced_block_and_prose_are_tolerated(self) -> None:
        text = "结果如下：\n```json\n" + _reply({"A": {"tags": ["站场"]}}) + "\n```\n以上。"
        rows, errors = tags_ai.parse_reply(text, ["A"])
        self.assertEqual(errors, [])
        self.assertEqual(rows["A"]["tags"], ["站场"])

    def test_bad_reply_reports_json_error(self) -> None:
        rows, errors = tags_ai.parse_reply("模型今天不想干活", ["A"])
        self.assertEqual(rows, {})
        self.assertTrue(any("JSON" in msg for msg in errors))

    def test_missing_rows_object_is_reported(self) -> None:
        rows, errors = tags_ai.parse_reply('{"tags": ["单解"]}', ["A"])
        self.assertEqual(rows, {})
        self.assertTrue(any("rows" in msg for msg in errors))

    def test_unknown_id_and_unknown_tag_are_reported(self) -> None:
        text = _reply({
            "X": {"tags": ["单解"]},
            "A": {"tags": ["魔法"]},
            "B": {"tags": ["治疗"]},
        })
        rows, errors = tags_ai.parse_reply(text, ["A", "B"])
        self.assertNotIn("X", rows)
        self.assertNotIn("A", rows)          # 标签全不认识，整行丢弃
        self.assertEqual(rows["B"]["tags"], ["治疗"])
        joined = "\n".join(errors)
        self.assertIn("未知 id：X", joined)
        self.assertIn("未知标签 魔法", joined)
        self.assertIn("漏了 1 张", joined)

    def test_note_is_capped(self) -> None:
        rows, _ = tags_ai.parse_reply(_reply({"A": {"tags": [], "note": "长" * 100}}), ["A"])
        self.assertEqual(len(rows["A"]["note"]), 60)


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.pending = self.dir / "card_tags_pending.jsonl"
        self.out = self.dir / "card_tags_ai.json"
        self.cfg = {"enabled": True, "api_key": "test-key", "base_url": "", "model": "fake"}

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _pending(self, count: int) -> None:
        lines = [
            json.dumps({"id": f"ID{index:03d}", "name": f"牌{index}", "cost": index % 10,
                        "type": "SPELL", "text": "造成2点伤害。", "why": "规则拿不准"},
                       ensure_ascii=False)
            for index in range(count)
        ]
        self.pending.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _fake_chat(self):
        """按 prompt 里的 id 回一批合法标签；记下每次调用。"""
        calls: list[list[dict]] = []

        def chat(messages, cfg, max_tokens=0):  # noqa: ANN001 - 对齐 ai.chat 签名
            calls.append(messages)
            ids = re.findall(r"^- ([A-Z0-9_]+)｜", messages[1]["content"], re.M)
            return _reply({card_id: {"tags": ["单解"], "note": "直伤"} for card_id in ids})

        chat.calls = calls  # type: ignore[attr-defined]
        return chat

    def test_no_key_prints_skip_and_returns_nonzero(self) -> None:
        self._pending(1)
        chat = self._fake_chat()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tags_ai.run(self.pending, self.out,
                               cfg={"enabled": True, "api_key": ""}, chat_fn=chat)
        self.assertEqual(code, 2)
        self.assertIn("AI 未配置，跳过", out.getvalue())
        self.assertIn("待定 1 张", out.getvalue())
        self.assertEqual(chat.calls, [])
        self.assertFalse(self.out.exists())

    def test_disabled_config_skips(self) -> None:
        self._pending(1)
        chat = self._fake_chat()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tags_ai.run(self.pending, self.out,
                               cfg={"enabled": False, "api_key": "k"}, chat_fn=chat)
        self.assertEqual(code, 2)
        self.assertIn("AI 已关闭", out.getvalue())
        self.assertEqual(chat.calls, [])
        self.assertFalse(self.out.exists())

    def test_empty_pending_needs_no_ai(self) -> None:
        self.pending.write_text("", encoding="utf-8")
        chat = self._fake_chat()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tags_ai.run(self.pending, self.out, cfg=self.cfg, chat_fn=chat)
        self.assertEqual(code, 0)
        self.assertIn("为空", out.getvalue())
        self.assertEqual(chat.calls, [])
        self.assertFalse(self.out.exists())

    def test_missing_pending_returns_nonzero(self) -> None:
        chat = self._fake_chat()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tags_ai.run(self.pending, self.out, cfg=self.cfg, chat_fn=chat)
        self.assertEqual(code, 2)
        self.assertIn("没有待定清单", out.getvalue())
        self.assertEqual(chat.calls, [])

    def test_batches_of_40_and_history_is_kept(self) -> None:
        self._pending(45)
        self.out.write_text(
            json.dumps({"version": 1, "rows": {"OLD_1": {"tags": ["护甲"], "note": "历史"}}},
                       ensure_ascii=False),
            encoding="utf-8")
        chat = self._fake_chat()
        with contextlib.redirect_stdout(io.StringIO()):
            code = tags_ai.run(self.pending, self.out, cfg=self.cfg, chat_fn=chat)
        self.assertEqual(code, 0)
        self.assertEqual(len(chat.calls), 2)
        first = re.findall(r"^- ([A-Z0-9_]+)｜", chat.calls[0][1]["content"], re.M)
        second = re.findall(r"^- ([A-Z0-9_]+)｜", chat.calls[1][1]["content"], re.M)
        self.assertEqual((len(first), len(second)), (40, 5))
        saved = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 1)
        self.assertEqual(len(saved["rows"]), 46)          # 45 张新的 + 1 张历史
        self.assertIn("OLD_1", saved["rows"])
        self.assertEqual(saved["rows"]["ID000"]["tags"], ["单解"])
        self.assertEqual(list(saved["rows"]), sorted(saved["rows"]))  # 排序确定

    def test_retries_once_then_keeps_success(self) -> None:
        self._pending(1)
        calls: list[list[dict]] = []

        def chat(messages, cfg, max_tokens=0):  # noqa: ANN001
            calls.append(messages)
            if len(calls) == 1:
                return "坏回复：不是 JSON"
            return _reply({"ID000": {"tags": ["斩杀"]}})

        with contextlib.redirect_stdout(io.StringIO()):
            code = tags_ai.run(self.pending, self.out, cfg=self.cfg, chat_fn=chat)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        saved = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(saved["rows"]["ID000"]["tags"], ["斩杀"])

    def test_all_replies_bad_writes_nothing(self) -> None:
        self._pending(2)

        def chat(messages, cfg, max_tokens=0):  # noqa: ANN001
            return "还是坏回复"

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tags_ai.run(self.pending, self.out, cfg=self.cfg, chat_fn=chat)
        self.assertEqual(code, 1)
        self.assertFalse(self.out.exists())
        self.assertIn("不写文件", out.getvalue())


class ValidateFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "card_tags_manual.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, payload: dict) -> None:
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_good_file_passes_and_empty_tags_is_only_a_warning(self) -> None:
        self._write({"version": 1, "rows": {
            "A": {"tags": ["单解", "斩杀"], "note": "x"},
            "B": {"tags": []},
        }})
        report = tags_ai.validate_file(self.path, known_ids={"A", "B"})
        self.assertTrue(tags_ai.validate_ok(report))
        self.assertEqual(report["rows"], 2)
        self.assertEqual(report["empty_tags"], ["B"])

    def test_unknown_id_and_unknown_tag_fail(self) -> None:
        self._write({"version": 1, "rows": {
            "Z": {"tags": ["单解"]},
            "A": {"tags": ["魔法"]},
        }})
        report = tags_ai.validate_file(self.path, known_ids={"A"})
        self.assertFalse(tags_ai.validate_ok(report))
        self.assertEqual(report["unknown_ids"], ["Z"])
        self.assertEqual(report["unknown_tags"], {"魔法": 1})
        self.assertIn("A", report["empty_tags"])

    def test_duplicate_keys_are_detected(self) -> None:
        self.path.write_text(
            '{"version": 1, "rows": {"A": {"tags": ["单解"]}, "A": {"tags": ["群解"]}}}',
            encoding="utf-8")
        report = tags_ai.validate_file(self.path, known_ids={"A"})
        self.assertEqual(report["duplicate_keys"], ["A"])
        self.assertFalse(tags_ai.validate_ok(report))

    def test_duplicate_tags_are_reported_but_pass(self) -> None:
        self._write({"version": 1, "rows": {"A": {"tags": ["单解", "单解"]}}})
        report = tags_ai.validate_file(self.path, known_ids={"A"})
        self.assertEqual(report["duplicate_tags"], {"A": ["单解"]})
        self.assertTrue(tags_ai.validate_ok(report))

    def test_id_check_is_case_insensitive(self) -> None:
        self._write({"version": 1, "rows": {"cs2_029": {"tags": ["单解"]}}})
        report = tags_ai.validate_file(self.path, known_ids={"CS2_029"})
        self.assertEqual(report["unknown_ids"], [])

    def test_utf8_bom_file_is_accepted(self) -> None:
        # 人工文件常被记事本存成带 BOM 的 UTF-8，BOM 不该让整份文件判成坏 JSON
        self.path.write_bytes(
            b"\xef\xbb\xbf" + json.dumps(
                {"version": 1, "rows": {"A": {"tags": ["单解"]}}}, ensure_ascii=False).encode("utf-8"))
        report = tags_ai.validate_file(self.path, known_ids={"A"})
        self.assertTrue(tags_ai.validate_ok(report))

    def test_bad_json_and_missing_rows_report_error(self) -> None:
        self.path.write_text("不是 JSON", encoding="utf-8")
        self.assertEqual(tags_ai.validate_file(self.path)["error"], "不是合法 JSON")
        self._write({"version": 1})
        self.assertIn("rows", tags_ai.validate_file(self.path)["error"])

    def test_missing_file_reports_error(self) -> None:
        report = tags_ai.validate_file(self.dir / "missing.json")
        self.assertIn("读不到", report["error"])
        self.assertFalse(tags_ai.validate_ok(report))


if __name__ == "__main__":
    unittest.main()
