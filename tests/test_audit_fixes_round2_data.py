"""B 组 round2 修复的数据层回归用例。

只覆盖 .audit_fix_spec2.md 的 B1–B6：tag 白名单、jsonl 原子截断、
同名不同 id 的卡组归一、按职业缓存被动规则、英雄技能可用/已用文案、
以及 state_types / ai 的既有契约（G2 顺带钉住）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import advisor, ai, decks as decks_mod, memory, powerlog as pl
from hsai.advisor import board_from_state
from hsai.cards import Card, CardDB
from hsai.decks import SavedDeck
from hsai.state_types import Side


class SideAsDictTest(unittest.TestCase):
    """G2：快照 Side 键集合与墓地只给数量，别把整份列表塞进去。"""

    EXPECTED = {
        "class_cn", "class_en", "health", "armor", "mana", "max_mana",
        "hand", "hand_count", "board", "deck_count", "deck_estimated",
        "secrets", "quests", "ongoing", "weapon", "hero_power",
        "hero_power_used", "graveyard_count",
    }

    def test_keys_and_defaults(self) -> None:
        d = Side(player_id=1).as_dict()
        self.assertEqual(set(d), self.EXPECTED)
        self.assertFalse(d["hero_power_used"])
        self.assertNotIn("graveyard", d)

    def test_graveyard_only_count(self) -> None:
        s = Side(player_id=1, graveyard=[{"id": "a"}, {"id": "b"}, {"id": "c"}])
        d = s.as_dict()
        self.assertEqual(d["graveyard_count"], 3)
        self.assertNotIn("graveyard", d)


class BuildPromptHeroPowerTest(unittest.TestCase):
    """G2：英雄技能明确区分「可用 / 已用」，且整段 prompt 不漏出 None。"""

    def _snap(self, used: bool) -> dict:
        return {
            "turn": 1,
            "is_our_turn": True,
            "our": {
                "class_cn": "法师", "health": 30, "armor": 0,
                "mana": 1, "max_mana": 1, "deck_count": 25,
                "hand": [], "board": [],
                "hero_power": {"name": "火焰冲击", "cost": 2, "text": "造成1点伤害"},
                "hero_power_used": used,
            },
            "opp": {
                "class_cn": "战士", "health": 30, "armor": 0,
                "hand_count": 3, "deck_count": 26, "board": [],
            },
        }

    def test_available_used_and_no_none(self) -> None:
        free = ai.build_prompt(self._snap(False))
        used = ai.build_prompt(self._snap(True))
        self.assertIn("可用", free)
        self.assertIn("已用", used)
        self.assertNotIn("None", free)
        self.assertNotIn("None", used)


class TagKeyFilterTest(unittest.TestCase):
    """B1：KNOWN_TAGS 是白名单，未知名字/数字一律过滤掉。"""

    def test_known_and_unknown(self) -> None:
        self.assertEqual(pl.tag_key("ZONE"), "ZONE")
        for tag in ("STEP", "NUM_CARDS_DRAWN_THIS_TURN", "FATIGUE", "WINDFURY"):
            self.assertIsNotNone(pl.tag_key(tag), f"{tag} 必须放行")
        self.assertIsNone(pl.tag_key("TOTALLY_UNKNOWN_TAG"))
        self.assertIsNone(pl.tag_key("999999"))

    def test_parse_text_drops_unknown_tag(self) -> None:
        text = "\n".join([
            "D 10:00:00.0000000 GameState.DebugPrintPower() - "
            "TAG_CHANGE Entity=6 tag=TOTALLY_UNKNOWN_TAG value=1",
            "D 10:00:00.0000001 GameState.DebugPrintPower() - "
            "TAG_CHANGE Entity=6 tag=ZONE value=HAND",
        ])
        events = pl.parse_text(text)
        self.assertFalse(any(
            ev.kind == "tag_change" and ev.data.get("tag") == "TOTALLY_UNKNOWN_TAG"
            for ev in events
        ))
        self.assertTrue(any(
            ev.kind == "tag_change" and ev.data.get("tag") == "ZONE"
            for ev in events
        ))


class AtomicTruncateTest(unittest.TestCase):
    """B2：截断走 tmp + os.replace，不留半截 jsonl、不留 .tmp。"""

    def test_truncate_is_atomic(self) -> None:
        cap = 3
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rows.jsonl"
            memory._append_jsonl(path, [{"i": i} for i in range(5)], cap)
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                json.loads(line)                       # 半截 jsonl 会在这里炸，正是原子写要防的
            self.assertEqual(len(lines), cap)
            self.assertEqual(list(Path(d).glob("*.tmp")), [])


class SiblingNormalizeTest(unittest.TestCase):
    """B3：同名不同 id（CS2_029 / CORE_CS2_029）按牌名归一后才算命中。"""

    def test_cards_db_matches_across_versions(self) -> None:
        db = CardDB([
            Card(id="CS2_029", name="火球术", type="SPELL"),
            Card(id="CORE_CS2_029", name="火球术", type="SPELL"),
        ])
        d1 = SavedDeck("1", "火球法A", "code", "MAGE", {"CS2_029": 2})
        d2 = SavedDeck("2", "火球法B", "code", "MAGE", {"CS2_029": 2, "OTHER": 1})
        seen = ["CORE_CS2_029"]

        _, score_with_db = decks_mod.best_match([d1, d2], seen, "MAGE", db)
        _, score_hard = decks_mod.best_match([d1, d2], seen, "MAGE")

        self.assertGreater(score_with_db, 0)
        self.assertEqual(score_hard, 0.0)


class PassiveRulePerClassTest(unittest.TestCase):
    """B4：被动规则按职业键缓存，两个职业互不覆盖。"""

    def setUp(self) -> None:
        advisor._PASSIVE_RULE.clear()

    def tearDown(self) -> None:
        advisor._PASSIVE_RULE.clear()

    def test_two_classes_coexist(self) -> None:
        db = CardDB([
            Card(id="HP_MAGE", name="火焰冲击", type="HERO_POWER", card_class="MAGE"),
            Card(id="HP_WARRIOR", name="叠甲", type="HERO_POWER", card_class="WARRIOR"),
        ])

        def make_state(class_en: str, hp_id: str):
            ours = Side(player_id=1, class_en=class_en,
                        hero_power={"id": hp_id}, deck_count=0)
            opp = Side(player_id=2, class_en="NEUTRAL", hand_count=0, deck_count=0)

            class St:
                cards = db
                turn = 1
                our_id = 1

                def sides(self):
                    return ours, opp

                def effective_turn_owner(self):
                    return 1

            return St()

        def fake_passive_rule(hp_card):
            return {"kind": hp_card.card_class.lower() + "_rule"}

        with mock.patch("hsai.advisor.policy.passive_rule",
                        side_effect=fake_passive_rule):
            b_mage = board_from_state(make_state("MAGE", "HP_MAGE"))
            b_warrior = board_from_state(make_state("WARRIOR", "HP_WARRIOR"))

        self.assertEqual(b_mage.sides[0].passive_rule, {"kind": "mage_rule"})
        self.assertEqual(b_warrior.sides[0].passive_rule, {"kind": "warrior_rule"})
        self.assertEqual(set(advisor._PASSIVE_RULE), {"MAGE", "WARRIOR"})
        self.assertTrue(all(isinstance(v, dict)
                            for v in advisor._PASSIVE_RULE.values()))


if __name__ == "__main__":
    unittest.main()
