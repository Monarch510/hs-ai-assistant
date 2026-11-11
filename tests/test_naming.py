"""卡组命名：按稀有度优先，同稀有度比出牌率，只取一张牌的名字。

为什么单独测：命名看着是小事，但它决定 `meta list` 能不能一眼认出「哦是那套」。
按出牌率取名字时，1 费普通牌永远排第一（几乎每套牌都带），学出来的原型就变成
「战士·斩杀」「牧师·神圣新星」—— 名字里没有任何能区分卡组的信息。
"""

from __future__ import annotations

import unittest

from hsai import cards as cards_mod
from hsai import deckcode
from hsai.meta import build_from_observations, key_card_rank, pick_key_cards

# 一组固定的战士牌：1 费普通 × 3 + 2 费稀有 + 3 费传说
COMMON_1 = "CORE_CS2_108"     # 斩杀 1费 普通
COMMON_2 = "CORE_EX1_391"     # 猛击 1费 普通
COMMON_3 = "CORE_EX1_400"     # 旋风斩 1费 普通
RARE_2 = "CORE_EX1_402"       # 铸甲师 2费 稀有
LEGENDARY_3 = "ETC_121"       # 摇滚教父沃恩 3费 传说

class KeyCardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()

    def test_legendary_beats_higher_frequency_common(self) -> None:
        """1 费普通牌 100% 出现，传说牌只有 70% —— 名字该取传说。

        稀有度是硬优先级：出牌率再高也不能把普通牌顶到传说前面。
        """
        keys = pick_key_cards(
            {COMMON_1: 2, LEGENDARY_3: 2}, self.db,
            presence={COMMON_1: 1.0, LEGENDARY_3: 0.7},
        )
        self.assertEqual(keys[0], LEGENDARY_3)

    def test_epic_beats_rare_beats_common(self) -> None:
        """三档稀有度同时在场，出牌率一样时按 传说 > 史诗 > 稀有 > 普通。"""
        epic = next(c for c in self.db.by_id.values()
                    if c.rarity == "EPIC" and c.collectible and c.name
                    and c.card_class in ("WARRIOR", "NEUTRAL") and c.cost <= 5)
        keys = pick_key_cards({COMMON_1: 2, RARE_2: 2, epic.id: 2}, self.db,
                              presence={COMMON_1: 1.0, RARE_2: 1.0, epic.id: 1.0})
        self.assertEqual(keys[:3], [epic.id, RARE_2, COMMON_1])

    def test_rarity_outranks_cost(self) -> None:
        """便宜的传说压过昂贵的史诗 —— 费用不是第一优先级。

        这正是「加权乘积」做不到的：乘积会让 8 费史诗压过 3 费传说，
        而规则是「有传说就用传说」。
        """
        legend = self.db.get(LEGENDARY_3)          # 3 费传说
        big_epic = next(c for c in self.db.by_id.values()
                        if c.rarity == "EPIC" and c.cost >= 8 and c.collectible and c.name)
        self.assertGreater(big_epic.cost, legend.cost)
        keys = pick_key_cards({legend.id: 1, big_epic.id: 2}, self.db,
                              presence={legend.id: 1.0, big_epic.id: 1.0})
        self.assertEqual(keys[0], legend.id)

    def test_same_rarity_sorted_by_presence(self) -> None:
        a = self.db.get(COMMON_1)
        b = self.db.get(COMMON_2)
        keys = pick_key_cards({a.id: 2, b.id: 2}, self.db,
                              presence={a.id: 0.5, b.id: 0.9})
        self.assertEqual(keys[0], b.id, "同为普通，出牌率高的先")

    def test_single_copy_legendary_is_not_penalised(self) -> None:
        """传说牌只能带 1 张，不能因为「没满编 2 张」就被判成边缘牌。"""
        keys = pick_key_cards({COMMON_1: 2, LEGENDARY_3: 1}, self.db)
        self.assertEqual(keys[0], LEGENDARY_3)

    def test_rank_is_lexicographic(self) -> None:
        legend = self.db.get(LEGENDARY_3)
        cheap = self.db.get(COMMON_1)
        self.assertLess(key_card_rank(legend, 0.1), key_card_rank(cheap, 1.0),
                        "传说就算出牌率 10% 也要排在普通牌前面")

    def test_order_is_reproducible(self) -> None:
        """同样的牌，插入顺序不同也必须排出同样的名次 —— 否则重建两次名字会变。"""
        a = pick_key_cards({COMMON_1: 2, COMMON_2: 2, LEGENDARY_3: 1}, self.db)
        b = pick_key_cards({LEGENDARY_3: 1, COMMON_2: 2, COMMON_1: 2}, self.db)
        self.assertEqual(a, b)

    def test_unknown_cards_are_skipped(self) -> None:
        self.assertEqual(pick_key_cards({"NOT_A_REAL_CARD": 2}, self.db), [])

class LearnedNameTest(unittest.TestCase):
    """聚类出来的原型，名字取关键牌。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()

    def _cluster(self, deck: list[str], n: int = 3) -> list[dict]:
        return [{"opp_class": "WARRIOR", "revealed": deck}] * n

    def test_name_uses_key_card_not_most_frequent(self) -> None:
        obs = self._cluster([COMMON_1, COMMON_2, LEGENDARY_3])
        meta = build_from_observations(obs, self.db, min_games=3)
        self.assertEqual(len(meta), 1)
        name = meta.archetypes[0].name
        self.assertIn("摇滚教父沃恩", name)
        self.assertNotIn("斩杀", name, "1 费普通牌出牌率最高，但不该拿来命名")

    def test_two_decks_sharing_a_legendary_get_different_names(self) -> None:
        """同职业两套牌共用同一张招牌牌时，名字必须区分开。

        名字重复会让 opp_history 的先验记到两套牌头上（它是按名字匹配的）。
        撞名时**不拼名字**，而是往后挪一张候选牌。
        """
        a = [LEGENDARY_3, COMMON_1, COMMON_2]
        b = [LEGENDARY_3, COMMON_3, RARE_2]
        meta = build_from_observations(self._cluster(a) + self._cluster(b),
                                       self.db, min_games=3, threshold=0.34)
        names = [x.name for x in meta]
        self.assertEqual(len(names), 2, f"两套牌应该分成两个簇，实际：{names}")
        self.assertEqual(len(set(names)), 2, f"名字撞了：{names}")
        self.assertIn("战士·摇滚教父沃恩", names, "先到的那套用第一张关键牌")
        self.assertNotIn("/", "".join(names), "名字里不该出现拼接的第二张牌")
        self.assertTrue(all(n.count("·") == 1 for n in names), names)

    def test_same_rarity_falls_back_to_play_rate(self) -> None:
        """没有传说/史诗时，退到稀有；稀有也没有就退到普通，同档比出牌率。"""
        obs = self._cluster([COMMON_1, COMMON_2, RARE_2])
        meta = build_from_observations(obs, self.db, min_games=3)
        name = meta.archetypes[0].name
        self.assertIn("铸甲师", name, "稀有牌优先于普通牌，哪怕出牌率一样")

    def test_name_is_stable_across_rebuilds(self) -> None:
        obs = self._cluster([COMMON_1, COMMON_2, LEGENDARY_3])
        first = build_from_observations(obs, self.db, min_games=3).archetypes[0].name
        second = build_from_observations(obs, self.db, min_games=3).archetypes[0].name
        self.assertEqual(first, second)

class DeckCodeNameTest(unittest.TestCase):
    """没给名字的卡组代码，也按关键牌起名。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()

    def test_auto_name_uses_key_card(self) -> None:
        legend = next(c for c in self.db.by_id.values()
                      if c.card_class == "MAGE" and c.rarity == "LEGENDARY"
                      and c.collectible and c.dbf and c.name)
        filler = [c for c in self.db.by_id.values()
                  if c.card_class == "MAGE" and c.rarity == "COMMON"
                  and c.collectible and c.dbf and c.name and c.cost <= 2][:5]
        self.assertTrue(filler)
        hero = next((c.dbf for c in self.db.by_id.values()
                     if c.id.startswith("HERO_08") and c.dbf), 7)

        counts = {legend.dbf: 1}
        for c in filler:
            counts[c.dbf] = 2
        resolved = deckcode.resolve(
            deckcode.DeckCode(fmt=2, heroes=[hero], cards=counts), self.db)

        self.assertIn(legend.name, resolved.name,
                      f"自动名应该用关键牌，实际是「{resolved.name}」")
        self.assertTrue(resolved.name.startswith("法师·"))

    def test_given_name_is_kept(self) -> None:
        """用户/游戏客户端给的名字要保留 —— 那是他自己起的。"""
        legend = next(c for c in self.db.by_id.values()
                      if c.card_class == "MAGE" and c.rarity == "LEGENDARY"
                      and c.collectible and c.dbf and c.name)
        hero = next((c.dbf for c in self.db.by_id.values()
                     if c.id.startswith("HERO_08") and c.dbf), 7)
        resolved = deckcode.resolve(
            deckcode.DeckCode(fmt=2, heroes=[hero], cards={legend.dbf: 1}),
            self.db, name="我的法术法")
        self.assertEqual(resolved.name, "我的法术法")

if __name__ == "__main__":
    unittest.main()
