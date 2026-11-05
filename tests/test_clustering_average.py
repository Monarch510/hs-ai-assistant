"""聚类连接方式：A~B、B~C 但 A≁C 时，三套牌不能被并成一簇。

单链判据（与簇内任一成员相似就并）会让 B 当桥，签名只剩中间那套的牌，
A 和 C 永远学不出来。这里用一组 Jaccard 可控的样本把平均连接钉住。

牌全部按索引从标准牌池取，不写死卡牌 id。
"""

from __future__ import annotations

import unittest

from hsai import cards as cards_mod
from hsai import meta


class AverageLinkageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.pool = [c.id for c in cls.db.standard_pool("WARRIOR")
                    if c.name and not cls.db.is_token(c.id)][:10]
        assert len(cls.pool) >= 8, "标准牌池里的 WARRIOR 牌不够构造样本"

    def _abc(self) -> tuple[set[str], set[str], set[str]]:
        p = self.pool
        # J(A,B) = 2/5 = 0.40，J(B,C) = 2/5 = 0.40，J(A,C) = 0
        return {p[0], p[1], p[2]}, {p[1], p[2], p[3], p[4]}, {p[3], p[4], p[5]}

    @staticmethod
    def _obs(sets: list[set[str]]) -> list[dict]:
        return [{"opp_class": "WARRIOR", "revealed": sorted(s)} for s in sets]

    def test_linkage_formula_differs_on_chained_samples(self) -> None:
        """同一组数据：旧判据 0.40 会并，新判据 0.20 不会并。"""
        A, B, C = self._abc()
        single = max(meta._jaccard(C, other) for other in (A, B))
        average = sum(meta._jaccard(C, other) for other in (A, B)) / 2
        self.assertAlmostEqual(single, 0.40)
        self.assertGreaterEqual(single, 0.34)
        self.assertAlmostEqual(average, 0.20)
        self.assertLess(average, 0.34)

    def test_average_linkage_splits_chained_samples(self) -> None:
        A, B, C = self._abc()
        db = meta.build_from_observations(
            self._obs([A, B, C]), self.db, min_games=1, threshold=0.34)
        self.assertEqual(len(db), 2, "修复前单链会把三套牌并成 1 簇")
        a1, c1 = self.pool[0], self.pool[5]
        self.assertTrue(any(a1 in arch.cards for arch in db))
        self.assertTrue(any(c1 in arch.cards for arch in db))
        self.assertFalse(any(a1 in arch.cards and c1 in arch.cards for arch in db))

    def test_identical_samples_stay_in_one_cluster(self) -> None:
        """平均连接不能把该合的拆开。"""
        A, _, _ = self._abc()
        db = meta.build_from_observations(
            self._obs([A, A, A]), self.db, min_games=1, threshold=0.34)
        self.assertEqual(len(db), 1)
        self.assertEqual(set(next(iter(db)).cards), A)

    def test_default_threshold_still_merges_close_samples(self) -> None:
        """不传 threshold 时默认仍是 0.34：J=0.375 的两套牌要合。"""
        p = self.pool
        X = {p[0], p[1], p[2], p[3], p[4]}
        Y = {p[2], p[3], p[4], p[5], p[6], p[7]}
        self.assertAlmostEqual(meta._jaccard(X, Y), 0.375)
        db = meta.build_from_observations(self._obs([X, Y]), self.db, min_games=1)
        self.assertEqual(len(db), 1)


if __name__ == "__main__":
    unittest.main()
