"""审计工具自己也得可信：用一组小 fixture 把口径钉死。

不依赖 data/card_tags.json（worker B 生成，测试环境可能还没出现），也不读真实
卡牌库 —— 否则真实数据一变，测试就跟着飘，而审计工具本身可能已经悄悄错了。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from hsai.cards import Card, CardDB

# tools/ 不是包，按文件路径加载
_SPEC = importlib.util.spec_from_file_location(
    "tags_audit", Path(__file__).resolve().parents[1] / "tools" / "tags_audit.py")
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def make_card(cid: str, name: str, cost: int = 0, type: str = "MINION",
              text: str = "", mechanics: tuple[str, ...] = ()) -> Card:
    return Card(id=cid, name=name, cost=cost, type=type, text=text, mechanics=mechanics)


def in_scope(card: Card, db: CardDB) -> bool:
    return card.type in {"MINION", "SPELL", "WEAPON", "LOCATION", "HERO"} and not card.id.upper().startswith("TOKEN")


# 覆盖：规则命中、0 标签、待定异常、越界各留一种
CARDS = [
    make_card("CS2_029", "火球术", 4, "SPELL", "造成$6点伤害。"),
    make_card("CS2_032", "暴风雪", 6, "SPELL", "对所有敌方随从造成$2点伤害，并使其冻结。"),
    make_card("EX1_001", "持盾步兵", 2, "MINION", "", ("TAUNT",)),
    make_card("CS2_182", "冰风雪人", 4, "MINION", ""),
    make_card("EX1_002", "盾牌格挡", 3, "SPELL", "获得$5点护甲，抽一张牌。"),
    make_card("EX1_003", "治疗波", 3, "SPELL", "恢复#5点生命值。"),
    make_card("EX1_004", "暗言术：毁", 4, "SPELL", "消灭一个随从。"),
    make_card("EX1_005", "奥术飞弹", 1, "SPELL", "对所有敌人造成$1点伤害。"),
    make_card("EX1_006", "治疗之触", 3, "SPELL", "恢复#8点生命值。"),
    make_card("EX1_007", "铁鬃灰熊", 3, "MINION", "", ("TAUNT",)),
    make_card("EX1_008", "炽炎战斧", 2, "WEAPON", ""),
    make_card("TOKEN_001", "英雄技能", 2, "HERO_POWER"),
]

ROWS = {
    "CS2_029": ("单解", "斩杀"),
    "CS2_032": ("群解", "减益"),
    "EX1_001": ("站场",),
    "CS2_182": ("站场",),
    "EX1_002": ("护甲", "抽牌"),
    "EX1_003": ("治疗",),
    "EX1_004": (),
    "EX1_005": (),
    "EX1_006": (),
    "EX1_007": (),
    "EX1_008": ("站场",),
}

PENDING = [
    {"id": "EX1_007", "name": "铁鬃灰熊", "why": "规则命中 0 标签"},
    {"id": "CS2_029", "name": "火球术", "why": "误入待定"},
    {"id": "EX1_002", "name": "盾牌格挡", "why": "奥秘：效果取决于对手动作"},
    {"id": "EX1_009", "name": "不存在", "text": "", "why": ""},
    {"id": "EX1_009", "name": "不存在", "text": "", "why": ""},
]


def build(rows=None, pending=None, **kwargs) -> dict:
    payload = {
        "rows": ROWS if rows is None else rows,
        "pending": PENDING if pending is None else pending,
    }
    return audit.collect_audit(payload, CardDB(CARDS), in_scope, **kwargs)


class AggregateTest(unittest.TestCase):
    def test_scope_excludes_out_of_scope_card(self) -> None:
        report = build()
        self.assertEqual(report["scope"]["total"], 11)
        ids = {row["id"] for _, rows in report["samples"] for row in rows}
        self.assertNotIn("TOKEN_001", ids, "HERO_POWER 不在 scope 内，不该进抽样")

    def test_type_distribution(self) -> None:
        self.assertEqual(
            build()["scope"]["by_type"],
            [("SPELL", 7), ("MINION", 3), ("WEAPON", 1)],
        )

    def test_tag_counts(self) -> None:
        counts = dict(build()["tag_counts"])
        self.assertEqual(counts["站场"], 3)
        self.assertEqual(counts["单解"], 1)
        self.assertEqual(counts["群解"], 1)
        self.assertEqual(counts["减益"], 1)
        self.assertEqual(counts["治疗"], 1)
        self.assertEqual(counts["护甲"], 1)
        self.assertEqual(counts["抽牌"], 1)
        self.assertEqual(counts["斩杀"], 1)
        self.assertEqual(counts["增益"], 0)
        self.assertEqual(counts["资源"], 0)
        self.assertEqual(list(counts), list(audit.TAG_ORDER), "输出顺序必须跟 TAG_ORDER 一致")

    def test_coverage_and_pending(self) -> None:
        cov = build()["coverage"]
        self.assertEqual(cov["total"], 11)
        self.assertEqual(cov["tagged"], 7)
        self.assertEqual(cov["untagged"], 4)
        self.assertEqual(cov["pending"], 3, "EX1_007、CS2_029、EX1_002 在 scope 且在待定清单")
        self.assertEqual(cov["untagged_not_pending"], 3)
        self.assertEqual(cov["coverage_pct"], 63.6)

    def test_top_pairs(self) -> None:
        pairs = dict(build()["pairs"])
        self.assertEqual(pairs[("单解", "斩杀")], 1)
        self.assertEqual(pairs[("群解", "减益")], 1)
        self.assertEqual(pairs[("护甲", "抽牌")], 1)
        self.assertEqual(len(pairs), 3)
        for a, b in pairs:
            self.assertLess(audit.TAG_ORDER.index(a), audit.TAG_ORDER.index(b),
                            "共现组合内标签要按 TAG_ORDER 排，否则同一组合会被拆成两条")

    def test_top_pairs_respects_top(self) -> None:
        self.assertEqual(len(build(top=1)["pairs"]), 1)

    def test_pending_ids_outside_scope_ignored(self) -> None:
        cov = build(pending=[{"id": "TOKEN_001", "why": "x"}])["coverage"]
        self.assertEqual(cov["pending"], 0)


class SamplingTest(unittest.TestCase):
    def test_groups_cover_all_tags_plus_untagged(self) -> None:
        groups = build(per_class=2)["samples"]
        self.assertEqual([label for label, _ in groups],
                         list(audit.TAG_ORDER) + ["（无标签）"])

    def test_sampling_cap_and_membership(self) -> None:
        groups = dict(build(per_class=2)["samples"])
        self.assertEqual(len(groups["站场"]), 2, "站场有 3 张，per_class=2 只抽 2 张")
        self.assertEqual(len(groups["单解"]), 1)
        self.assertEqual(len(groups["（无标签）"]), 2, "无标签有 4 张，只抽 2 张")
        self.assertEqual(groups["单解"][0]["id"], "CS2_029")
        for label, rows in groups.items():
            for row in rows:
                if label == "（无标签）":
                    self.assertEqual(row["tags"], (), f"{row['id']} 不该出现在无标签组")
                else:
                    self.assertIn(label, row["tags"], f"{row['id']} 不属于「{label}」")

    def test_fixed_seed_is_reproducible(self) -> None:
        first = build(per_class=2, seed=7)["samples"]
        second = build(per_class=2, seed=7)["samples"]
        self.assertEqual(first, second, "同 seed 两次抽样必须完全一样，否则人工核对无法复现")
        self.assertEqual(build(per_class=2, seed=8)["samples"],
                         build(per_class=2, seed=8)["samples"])


class SuspiciousTest(unittest.TestCase):
    def test_untagged_with_signal_words(self) -> None:
        suspicious = build()["suspicious"]
        self.assertEqual(suspicious["count"], 3)
        self.assertEqual(
            sorted(row["card"]["id"] for row in suspicious["samples"]),
            ["EX1_004", "EX1_005", "EX1_006"],
        )
        self.assertEqual(dict(suspicious["signals"]),
                         {"消灭": 1, "伤害": 1, "恢复": 1})

    def test_untagged_without_signal_is_not_suspicious(self) -> None:
        ids = {item["card"]["id"] for item in build()["suspicious"]["samples"]}
        self.assertNotIn("EX1_007", ids, "嘲讽随从没文本，不该进可疑清单")
        self.assertNotIn("CS2_182", ids, "白板随从有标签，不参与可疑清单")

    def test_untagged_is_limited(self) -> None:
        suspicious = build()
        self.assertLessEqual(len(suspicious["suspicious"]["samples"]), audit.SAMPLE_LIMIT)


class PendingTest(unittest.TestCase):
    def test_anomalies(self) -> None:
        pending = build()["pending"]
        self.assertEqual(pending["total"], 5)
        self.assertEqual(pending["tagged_secret_pending"]["count"], 1,
                         "奥秘牌有标签也照样进待定，这是口径要求的，不算冲突")
        self.assertEqual(pending["tagged_secret_pending"]["samples"][0]["id"], "EX1_002")
        self.assertEqual(pending["tagged_but_pending"]["count"], 1)
        self.assertEqual(pending["tagged_but_pending"]["samples"][0]["id"], "CS2_029")
        self.assertEqual(pending["blank_text"]["count"], 3)
        self.assertEqual(pending["unknown_card"]["count"], 2)
        self.assertEqual(pending["duplicate_ids"], {"count": 1, "samples": ["EX1_009"]})
        self.assertEqual(pending["missing_why"]["count"], 2)

    def test_empty_pending_is_fine(self) -> None:
        pending = build(pending=[])["pending"]
        self.assertEqual(pending["total"], 0)
        self.assertEqual(pending["tagged_but_pending"]["count"], 0)
        self.assertEqual(pending["tagged_secret_pending"]["count"], 0)


class StructuralTest(unittest.TestCase):
    def test_outside_scope_row(self) -> None:
        rows = dict(ROWS)
        rows["TOKEN_001"] = ("站场",)
        issues = build(rows=rows)["structural"]
        self.assertEqual(issues["outside_scope"], {"count": 1, "samples": ["TOKEN_001"]})

    def test_invalid_tag_name(self) -> None:
        rows = dict(ROWS)
        rows["CS2_182"] = ("站场", "身材")
        issues = build(rows=rows)["structural"]
        self.assertEqual(issues["invalid_tags"]["count"], 1)
        self.assertEqual(issues["invalid_tags"]["samples"], ["CS2_182:身材"])

    def test_duplicate_and_unsorted_tags(self) -> None:
        rows = dict(ROWS)
        rows["CS2_182"] = ("站场", "站场")
        issues = build(rows=rows)["structural"]
        self.assertEqual(issues["duplicate_tags"]["count"], 1)
        self.assertEqual(issues["unsorted_tags"]["count"], 1)

        rows = dict(ROWS)
        rows["EX1_002"] = ("抽牌", "护甲")
        issues = build(rows=rows)["structural"]
        self.assertEqual(issues["unsorted_tags"]["count"], 1)
        self.assertEqual(issues["duplicate_tags"]["count"], 0)


class NormalizeTest(unittest.TestCase):
    def test_accepts_object_and_list_rows(self) -> None:
        rows = audit.normalize_rows({
            "rows": {
                "cs2_029": ["斩杀", "单解"],
                "ex1_001": {"tags": ["站场"], "src": "rule"},
                "EX1_002": None,
            }
        })
        self.assertEqual(rows["CS2_029"], ("斩杀", "单解"))
        self.assertEqual(rows["EX1_001"], ("站场",))
        self.assertEqual(rows["EX1_002"], ())

    def test_clean_tags_sorts_and_dedupes(self) -> None:
        self.assertEqual(audit.clean_tags(("斩杀", "单解", "斩杀")), ("单解", "斩杀"))
        self.assertEqual(audit.clean_tags(("身材", "单解")), ("单解", "身材"),
                         "非法标签排最后，避免审计输出顺序乱")


class FormatTest(unittest.TestCase):
    def test_report_has_all_sections(self) -> None:
        text = audit.format_audit(build(per_class=1))
        for section in ("== 1. 范围 ==", f"== 2. {len(audit.TAG_ORDER)} 类计数 / 覆盖率 ==",
                        "== 3. 两两共现", "== 4. 抽样核对",
                        "== 5. 可疑清单", "== 6. 待定清单异常", "== 7. 结构问题"):
            self.assertIn(section, text)
        self.assertIn("火球术", text)
        self.assertIn("造成6点伤害", text, "文本要能人工读，标记符号应被去掉")

    def test_display_text_trims_markup_and_length(self) -> None:
        self.assertEqual(audit.display_text("<b>造成</b>$6点伤害"), "造成6点伤害")
        self.assertTrue(audit.display_text("伤害" * 100, limit=10).endswith("…"))


if __name__ == "__main__":
    unittest.main()
