"""卡牌功能标签：范围口径、规则+补标合并、确定性、读写、报告。

规则本身的判定由 worker A 的 tests/test_tag_rules.py 覆盖；这里用假 RULES 测
「范围与合并」这层，避免 A 改规则时这边的用例跟着红。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import tags
from hsai.cards import Card, CardDB
from hsai.tag_rules import TAG_ORDER


def _card(
    cid: str,
    *,
    name: str = "",
    type: str = "MINION",
    set_name: str = "",
    text: str = "",
    cost: int = 0,
    collectible: bool = True,
) -> Card:
    return Card(id=cid, name=name or cid, cost=cost, type=type, set=set_name,
                text=text, collectible=collectible)


def _rules(hits: dict[str, str]) -> dict[str, object]:
    """按「文本里含关键字」造一套规则，让测试不依赖 A 的具体判定。"""
    return {
        tag: (lambda card, needle=hits.get(tag): bool(needle) and needle in (card.text or ""))
        for tag in TAG_ORDER
    }


class ScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = CardDB([
            _card("SCOPE_MINION"),
            _card("SCOPE_SPELL", type="SPELL"),
            _card("SCOPE_WEAPON", type="WEAPON"),
            _card("SCOPE_LOCATION", type="LOCATION"),
            _card("SCOPE_HERO", type="HERO"),
            _card("SCOPE_TOKEN", collectible=False),
            _card("SCOPE_SUFFIX_T"),
            _card("SCOPE_HERO_SKIN", type="HERO", set_name="HERO_SKINS"),
            _card("SCOPE_CREDITS", set_name="CREDITS"),
            _card("SCOPE_HERO_POWER", type="HERO_POWER"),
            _card("SCOPE_ENCHANTMENT", type="ENCHANTMENT"),
        ])

    def _scope(self, cid: str) -> bool:
        return tags.in_scope(self.db.get(cid), self.db)

    def test_playable_types_in_scope(self) -> None:
        for cid in ("SCOPE_MINION", "SCOPE_SPELL", "SCOPE_WEAPON", "SCOPE_LOCATION", "SCOPE_HERO"):
            with self.subTest(cid=cid):
                self.assertTrue(self._scope(cid))

    def test_token_excluded(self) -> None:
        # 不可收集的衍生牌、以及 id 以 T 结尾的 token 都不算「打出的牌」
        for cid in ("SCOPE_TOKEN", "SCOPE_SUFFIX_T"):
            with self.subTest(cid=cid):
                self.assertFalse(self._scope(cid))

    def test_hero_skin_and_credits_excluded(self) -> None:
        for cid in ("SCOPE_HERO_SKIN", "SCOPE_CREDITS"):
            with self.subTest(cid=cid):
                self.assertFalse(self._scope(cid))

    def test_non_playable_types_excluded(self) -> None:
        for cid in ("SCOPE_HERO_POWER", "SCOPE_ENCHANTMENT"):
            with self.subTest(cid=cid):
                self.assertFalse(self._scope(cid))

    def test_event_system_card_and_essences(self) -> None:
        # 审计 #13：本体抽不到也打不出，不打标；6 份精华是会施放的法术，纳入范围
        cards = [_card("CATA_EVENT_110", type="SPELL", text="对战开始时：破碎成6份精华。")]
        cards += [_card(cid, type="SPELL", text="精华") for cid in sorted(tags.PLAYABLE_TOKEN_IDS)]
        db = CardDB(cards)
        self.assertFalse(tags.in_scope(db.get("CATA_EVENT_110"), db))
        for cid in tags.PLAYABLE_TOKEN_IDS:
            with self.subTest(cid=cid):
                self.assertTrue(db.is_token(cid), "id 结尾 T2..T7 本会被判衍生物")
                self.assertTrue(tags.in_scope(db.get(cid), db))


class ClassifyTest(unittest.TestCase):
    def test_result_follows_tag_order(self) -> None:
        rules = _rules({"群解": "群", "单解": "单"})
        with mock.patch.object(tags, "RULES", rules):
            self.assertEqual(tags.classify(_card("X", text="群单")), ("单解", "群解"))

    def test_no_hit_is_empty(self) -> None:
        with mock.patch.object(tags, "RULES", _rules({})):
            self.assertEqual(tags.classify(_card("X", text="随便什么")), ())


class BuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = CardDB([
            _card("AAA_SINGLE", text="造成单体伤害"),
            _card("BBB_BLANK", text=""),
            _card("CCC_BLANK", text=""),
            _card("DDD_TOKEN", text="召唤", collectible=False),
            _card("EEE_SKIN", type="HERO", set_name="HERO_SKINS"),
        ])
        self._rule_patch = mock.patch.object(tags, "RULES", _rules({"单解": "单体"}))
        self._pending_patch = mock.patch.object(tags, "_rule_pending_reason", lambda card: None)
        self._rule_patch.start()
        self._pending_patch.start()

    def tearDown(self) -> None:
        self._pending_patch.stop()
        self._rule_patch.stop()

    def test_build_shape_and_scope(self) -> None:
        payload = tags.build(self.db)
        self.assertEqual(set(payload), {"version", "generated_at", "scope", "rows", "pending"})
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["scope"], {"total": 3, "by_type": {"MINION": 3}})
        # 范围外的牌不进 rows
        self.assertNotIn("DDD_TOKEN", payload["rows"])
        self.assertNotIn("EEE_SKIN", payload["rows"])
        self.assertEqual(list(payload["rows"]), sorted(payload["rows"]))

    def test_event_essences_are_built_but_system_card_is_not(self) -> None:
        # 审计 #13：范围纠正后，本体不落盘、精华按规则打标落盘
        db = CardDB([
            _card("CATA_EVENT_110", type="SPELL", text="对战开始时：破碎成6份精华。"),
            _card("CATA_EVENT_110T2", type="SPELL", text="造成单体伤害"),
        ])
        rows = tags.build(db)["rows"]
        self.assertNotIn("CATA_EVENT_110", rows)
        self.assertEqual(rows["CATA_EVENT_110T2"], {"tags": ["单解"], "src": "rule"})

    def test_rule_tags(self) -> None:
        payload = tags.build(self.db)
        self.assertEqual(payload["rows"]["AAA_SINGLE"], {"tags": ["单解"], "src": "rule"})
        # 规则没命中也要留一行（允许空标签），src 仍是 rule
        self.assertEqual(payload["rows"]["BBB_BLANK"], {"tags": [], "src": "rule"})

    def test_ai_and_manual_union(self) -> None:
        ai = {
            "aaa_single": {"tags": ["斩杀", "不存在的标签"], "note": "AI 理由"},
            "bbb_blank": {"tags": ["资源"], "note": "AI 理由"},
        }
        manual = {
            "bbb_blank": {"tags": ["抽牌"], "note": "人工理由"},
            "ccc_blank": {"tags": ["增益"]},
        }
        rows = tags.build(self.db, ai=ai, manual=manual)["rows"]
        self.assertEqual(rows["AAA_SINGLE"], {"tags": ["单解", "斩杀"], "src": "rule+ai",
                                              "note": "AI 理由"})
        self.assertEqual(rows["BBB_BLANK"], {"tags": ["资源", "抽牌"], "src": "ai+manual",
                                             "note": "人工理由"})
        self.assertEqual(rows["CCC_BLANK"], {"tags": ["增益"], "src": "manual"})
        # 补标只加自己那份，不会把规则标签挤掉；未知标签被丢掉
        self.assertNotIn("不存在的标签", rows["AAA_SINGLE"]["tags"])

    def test_pending_sorted_and_in_scope_only(self) -> None:
        def why(card: Card) -> str | None:
            return "文本不明确" if card.id in ("CCC_BLANK", "DDD_TOKEN", "AAA_SINGLE") else None

        with mock.patch.object(tags, "_rule_pending_reason", why):
            payload = tags.build(self.db)
        # 待定按 id 排序；范围外的 DDD_TOKEN 不进清单
        self.assertEqual([p["id"] for p in payload["pending"]], ["AAA_SINGLE", "CCC_BLANK"])
        first = payload["pending"][0]
        self.assertEqual(set(first), {"id", "name", "cost", "type", "text", "why"})
        self.assertEqual(first["why"], "文本不明确")

    def test_pending_reason_passthrough(self) -> None:
        with mock.patch.object(tags, "_rule_pending_reason", lambda card: "拿不准"):
            self.assertEqual(tags.pending_reason(_card("X")), "拿不准")

    def test_build_is_deterministic(self) -> None:
        first = tags.build(self.db)
        second = tags.build(self.db)
        first.pop("generated_at")
        second.pop("generated_at")
        self.assertEqual(first, second)


class SaveLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "card_tags.json"
        self.payload = {
            "version": 1,
            "generated_at": "2026-11-06T23:00:00+08:00",
            "scope": {"total": 2, "by_type": {"MINION": 2}},
            "rows": {
                "AAA": {"tags": ["单解"], "src": "rule"},
                "BBB": {"tags": [], "src": "rule"},
            },
            "pending": [
                {"id": "BBB", "name": "B", "cost": 1, "type": "MINION", "text": "", "why": "x"},
            ],
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_save_and_load(self) -> None:
        path = tags.save(self.payload, self.path)
        self.assertEqual(path, self.path)
        self.assertEqual(tags.load(self.path), {"AAA": ("单解",), "BBB": ()})
        self.assertEqual(tags.load_payload(self.path), self.payload)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.payload)

    def test_save_creates_parent(self) -> None:
        nested = self.dir / "sub" / "card_tags.json"
        self.assertEqual(tags.save(self.payload, nested), nested)
        self.assertTrue(nested.exists())

    def test_load_missing_file_returns_empty(self) -> None:
        self.assertEqual(tags.load(self.dir / "nope.json"), {})
        self.assertEqual(tags.load_payload(self.dir / "nope.json"), {})

    def test_load_corrupt_file_returns_empty(self) -> None:
        bad = self.dir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(tags.load(bad), {})
        self.assertEqual(tags.load_payload(bad), {})

    def test_load_bom_file(self) -> None:
        # Windows 编辑器常给 json 加 BOM；读成「没有补标」会静默丢数据
        path = self.dir / "bom.json"
        path.write_text("\ufeff" + json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(tags.load(path)["AAA"], ("单解",))
        self.assertEqual(tags.load_source(path)["AAA"]["tags"], ["单解"])

    def test_save_pending_jsonl(self) -> None:
        path = tags.save_pending(self.payload, self.dir / "pending.jsonl")
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["id"], "BBB")

    def test_card_tags_query(self) -> None:
        tags.save(self.payload, self.path)
        self.assertEqual(tags.card_tags("AAA", path=self.path), ("单解",))
        self.assertEqual(tags.card_tags("aaa", path=self.path), ("单解",))
        self.assertEqual(tags.card_tags(_card("AAA"), path=self.path), ("单解",))
        self.assertEqual(tags.card_tags("未知", path=self.path), ())
        # 没 build 过就返回 ()，查询时不重建
        self.assertEqual(tags.card_tags("AAA", path=self.dir / "missing.json"), ())

    def test_card_tags_by_name(self) -> None:
        tags.save(self.payload, self.path)
        db = CardDB([_card("AAA", name="火球术")])
        self.assertEqual(tags.card_tags("火球术", cards=db, path=self.path), ("单解",))


class ReportTest(unittest.TestCase):
    def _payload(self) -> dict:
        return {
            "version": 1,
            "generated_at": "2026-11-06T23:00:00+08:00",
            "scope": {"total": 4, "by_type": {"MINION": 3, "SPELL": 1}},
            "rows": {
                "A": {"tags": ["单解", "斩杀"], "src": "rule"},
                "B": {"tags": [], "src": "rule"},
                "C": {"tags": ["资源"], "src": "rule+ai"},
                "D": {"tags": ["资源"], "src": "ai"},
            },
            "pending": [{"id": "X"}],
        }

    def test_summary_counts(self) -> None:
        s = tags.summary(self._payload())
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["by_type"], {"MINION": 3, "SPELL": 1})
        self.assertEqual(s["tags"]["单解"], 1)
        self.assertEqual(s["tags"]["斩杀"], 1)
        self.assertEqual(s["tags"]["资源"], 2)
        self.assertEqual(s["tags"]["群解"], 0)
        self.assertEqual(s["pending"], 1)
        self.assertEqual(s["src"], {"ai": 1, "rule": 2, "rule+ai": 1})
        self.assertEqual(s["untagged"], 1)

    def test_format_report(self) -> None:
        out = tags.format_report(self._payload())
        for word in ("范围：4 张", "随从 3", "法术 1", "每类张数",
                     "单解 1", "群解 0", "未打标签：1", "待定：1 张", "src 分布",
                     "rule+ai 1"):
            with self.subTest(word=word):
                self.assertIn(word, out)

    def test_format_report_empty(self) -> None:
        out = tags.format_report({})
        self.assertIn("范围：0 张", out)
        self.assertIn("待定：0 张", out)


if __name__ == "__main__":
    unittest.main()
