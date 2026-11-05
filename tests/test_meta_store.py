"""卡组原型的存储格式。

对应四条硬要求：
1. 内置示例原型（seed）已删除，不会再凭空冒出来
2. 一个原型一个文件：`<dir>/<id>.json`
3. `cards` 带名称、费用、张数
4. `cards` 按费用升序

这些断言看起来像在测「json 长什么样」，其实测的是**能不能人工核对和手改**：
只存 id 和张数时，想确认一套牌是不是记错了，得先去卡牌库里查 30 个 id。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hsai import cards as cards_mod
from hsai import meta


class StoreFormatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        pool = [c for c in cls.db.standard_pool("WARRIOR")
                if c.name and not cls.db.is_token(c.id)]
        # 故意按费用倒序放进去：写出来必须是升序，否则排序是「碰巧对的」
        pool.sort(key=lambda c: -(c.cost if isinstance(c.cost, int) else 0))
        cls.picked = pool[:6]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "meta"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _db(self) -> meta.MetaDB:
        return meta.MetaDB([
            meta.Archetype(id="tempo_warrior", name="节奏战", card_class="WARRIOR",
                           prior=0.6, cards={c.id: 2 for c in self.picked},
                           source="import"),
            meta.Archetype(id="ctrl_warrior", name="控制战", card_class="WARRIOR",
                           prior=0.4, cards={self.picked[0].id: 1}, source="import"),
        ], self.db)

    # ---------------------------------------------------------- 一个原型一个文件
    def test_one_file_per_archetype(self) -> None:
        self._db().save(self.dir)
        names = sorted(p.name for p in self.dir.glob("*.json"))
        self.assertEqual(names, ["ctrl_warrior.json", "tempo_warrior.json"])

    def test_removed_archetype_disappears_from_disk(self) -> None:
        """save() 会清掉不在内存里的文件 —— 删原型就等于删文件，不需要 prune。"""
        db = self._db()
        db.save(self.dir)
        db.archetypes = [a for a in db if a.id == "tempo_warrior"]
        db.save(self.dir)
        self.assertFalse((self.dir / "ctrl_warrior.json").exists(),
                         "从内存移除的原型必须从磁盘上消失，否则下次加载又冒出来")
        self.assertTrue((self.dir / "tempo_warrior.json").exists())

    def test_broken_file_is_not_deleted(self) -> None:
        """手改坏的文件要留着 —— 静默删掉等于吞用户数据。"""
        self.dir.mkdir(parents=True)
        (self.dir / "hand_edited.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
        self._db().save(self.dir)
        self.assertTrue((self.dir / "hand_edited.json").exists(),
                        "解析不了的文件不能被当成「已删除的原型」清掉")

    def test_roundtrip_keeps_counts(self) -> None:
        self._db().save(self.dir)
        back = meta.MetaDB.load(self.db, self.dir)
        self.assertEqual(len(back), 2)
        a = back.get("tempo_warrior")
        self.assertIsNotNone(a)
        self.assertEqual(a.cards, {c.id: 2 for c in self.picked})
        self.assertEqual(a.name, "节奏战")
        self.assertEqual(a.card_class, "WARRIOR")
        self.assertAlmostEqual(a.prior, 0.6, places=3)

    def test_prior_base_survives_roundtrip(self) -> None:
        """校准起点要能存下来 —— 丢了它，`meta calibrate` 每跑一次就更偏一次。"""
        # 两个原型的 prior 加起来正好是 1，normalize_priors 不会改动它们
        m = self._db()
        m.get("tempo_warrior").prior, m.get("tempo_warrior").prior_base = 0.25, 0.6
        m.get("ctrl_warrior").prior, m.get("ctrl_warrior").prior_base = 0.75, 0.4
        m.save(self.dir)
        back = meta.MetaDB.load(self.db, self.dir)
        self.assertAlmostEqual(back.get("tempo_warrior").prior, 0.25, places=3)
        self.assertAlmostEqual(back.get("tempo_warrior").prior_base, 0.6, places=3)
        self.assertAlmostEqual(back.get("ctrl_warrior").prior_base, 0.4, places=3)

    def test_old_file_without_prior_base_falls_back_to_prior(self) -> None:
        """老文件没有 prior_base，读出来要能退化用 prior 当起点，而不是 0。"""
        self.dir.mkdir(parents=True)
        (self.dir / "old.json").write_text(json.dumps({
            "id": "old", "name": "老格式", "class": "WARRIOR", "prior": 0.4,
            "cards": {self.picked[0].id: 2},
        }, ensure_ascii=False), encoding="utf-8")
        back = meta.MetaDB.load(self.db, self.dir).get("old")
        self.assertIsNone(back.prior_base)
        self.assertAlmostEqual(back.base_prior, back.prior, places=6)

    # ---------------------------------------------------------- cards 字段
    def test_cards_carry_name_cost_count(self) -> None:
        self._db().save(self.dir)
        payload = json.loads((self.dir / "tempo_warrior.json").read_text(encoding="utf-8"))
        rows = payload["cards"]
        self.assertIsInstance(rows, list, "cards 应该是数组，不是 {id: 张数}")
        for row in rows:
            self.assertEqual(set(row), {"id", "name", "cost", "count"})
            self.assertTrue(row["name"], f"{row['id']} 缺中文名，没法人工核对")
            self.assertIsInstance(row["cost"], int)
            self.assertEqual(row["count"], 2)

    def test_cards_sorted_by_cost_ascending(self) -> None:
        self._db().save(self.dir)
        rows = json.loads((self.dir / "tempo_warrior.json").read_text(encoding="utf-8"))["cards"]
        costs = [r["cost"] for r in rows]
        self.assertEqual(costs, sorted(costs), "cards 必须按费用从小到大")
        # 同一份数据重复写出来要逐字一致（否则 diff 全是噪音）
        first = (self.dir / "tempo_warrior.json").read_text(encoding="utf-8")
        self._db().save(self.dir)
        self.assertEqual(first, (self.dir / "tempo_warrior.json").read_text(encoding="utf-8"))

    def test_legacy_card_dict_still_reads(self) -> None:
        """老文件的 cards 是 {id: 张数}，升级后不能读不出来。"""
        self.dir.mkdir(parents=True)
        (self.dir / "old.json").write_text(json.dumps({
            "id": "old", "name": "老格式", "class": "WARRIOR", "prior": 1.0,
            "cards": {self.picked[0].id: 2, self.picked[1].id: 1},
            "source": "import",
        }, ensure_ascii=False), encoding="utf-8")
        back = meta.MetaDB.load(self.db, self.dir)
        self.assertEqual(back.get("old").cards, {self.picked[0].id: 2, self.picked[1].id: 1})

    # ---------------------------------------------------------- 老单文件迁移
    def test_single_file_is_migrated_in_place(self) -> None:
        legacy = Path(self._tmp.name) / "meta.json"
        legacy.write_text(json.dumps({
            "version": 1,
            "archetypes": [
                {"id": "a1", "name": "一套", "class": "WARRIOR", "prior": 0.5,
                 "cards": {self.picked[0].id: 2}, "source": "import"},
                {"id": "a2", "name": "二套", "class": "WARRIOR", "prior": 0.5,
                 "cards": {self.picked[1].id: 2}, "source": "import"},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        db = meta.MetaDB.load(self.db, legacy)
        self.assertEqual(len(db), 2)
        # 拆到同名目录里，原文件改名留档 —— 不删用户数据
        self.assertTrue((Path(self._tmp.name) / "meta" / "a1.json").exists())
        self.assertFalse(legacy.exists())
        self.assertTrue(legacy.with_suffix(".json.migrated").exists())
        # 迁移时就要补上名称/费用，别等下一次 save
        rows = json.loads((Path(self._tmp.name) / "meta" / "a1.json")
                          .read_text(encoding="utf-8"))["cards"]
        self.assertTrue(rows[0]["name"])

    # ---------------------------------------------------------- 种子
    def test_no_builtin_seeds(self) -> None:
        """内置示例原型已删：删掉它们是因为它们不是真实天梯数据。"""
        self.assertFalse(hasattr(meta, "SEED"), "SEED 列表应该已经删掉")
        self.assertFalse(hasattr(meta, "seeds"), "seeds() 应该已经删掉")
        self.assertEqual(len(meta.build_from_observations([], self.db)), 0,
                         "没有对局记录时不该凭空长出原型")


if __name__ == "__main__":
    unittest.main()
