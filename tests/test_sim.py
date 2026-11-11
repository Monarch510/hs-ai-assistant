"""自对弈与自学习。"""

from __future__ import annotations

import unittest

from hsai import cards as cards_mod
from hsai import learn, sim
from hsai.meta import Archetype, MetaDB, build_from_observations


def _arch(db, cls: str, name: str, ids: list[str], prior: float = 0.5) -> Archetype:
    """用职业牌池里的几张牌拼一个原型（只当标志牌用，其余槽位由 make_deck 补）。"""
    return Archetype(id=f"{cls.lower()}_{name}", name=name, card_class=cls,
                     prior=prior, cards={c: 2 for c in ids}, source="import")


def _fixture_meta(db) -> MetaDB:
    """两个自建原型。不读 data/meta/ —— 那里的内容取决于用户导入了什么。"""
    mage = [c.id for c in db.standard_pool("MAGE") if c.name and not db.is_token(c.id)][:5]
    war = [c.id for c in db.standard_pool("WARRIOR") if c.name and not db.is_token(c.id)][:5]
    return MetaDB([_arch(db, "MAGE", "节奏法", mage),
                   _arch(db, "WARRIOR", "节奏战", war)], db)


class SimTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.meta = _fixture_meta(cls.db)

    def test_deck_size(self) -> None:
        import random

        deck = sim.make_deck(self.meta.get("mage_节奏法"), self.db, random.Random(0))
        self.assertEqual(len(deck), 30)

    def test_game_finishes_and_is_deterministic(self) -> None:
        import random

        da = sim.make_deck(self.meta.get("mage_节奏法"), self.db, random.Random(0))
        db_ = sim.make_deck(self.meta.get("warrior_节奏战"), self.db, random.Random(1))
        a = sim.run_game(da, db_, self.db, seed=5, max_turns=40)
        b = sim.run_game(da, db_, self.db, seed=5, max_turns=40)
        self.assertEqual((a.winner, a.turns), (b.winner, b.turns))
        self.assertIsNotNone(a.winner, "40 回合内应该分出胜负")

    def test_match_returns_winrates(self) -> None:
        res = sim.match(self.meta.get("mage_节奏法"), self.meta.get("warrior_节奏战"),
                        self.db, games=4, seed=2)
        self.assertEqual(res["games"], 4)
        self.assertAlmostEqual(res["winrate_a"] + res["winrate_b"], 1.0, places=6)


class LearnTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.meta = _fixture_meta(cls.db)

    def test_bootstrap_clusters(self) -> None:
        obs = [
            {"opp_class": "WARRIOR", "revealed": ["NEW1_025", "CORE_EX1_604", "CORE_EX1_046"]},
            {"opp_class": "WARRIOR", "revealed": ["NEW1_025", "CORE_EX1_604"]},
            {"opp_class": "WARRIOR", "revealed": ["NEW1_025", "CORE_EX1_046"]},
            {"opp_class": "WARRIOR", "revealed": ["CORE_EX1_407", "CORE_CS2_108", "CORE_EX1_410"]},
            {"opp_class": "WARRIOR", "revealed": ["CORE_EX1_407", "CORE_CS2_108"]},
            {"opp_class": "WARRIOR", "revealed": ["CORE_EX1_407", "CORE_EX1_410"]},
        ]
        db = build_from_observations(obs, self.db, min_games=2, threshold=0.3)
        self.assertEqual(len(db), 2)
        self.assertTrue(all(a.card_class == "WARRIOR" for a in db))
        self.assertAlmostEqual(sum(a.prior for a in db), 1.0, places=6)

    def test_mirror_winrate_is_near_half_for_same_weights(self) -> None:
        w = dict(learn.DEFAULT_WEIGHTS)
        wr, n = learn.mirror_winrate(self.db, self.meta, w, dict(w), games=1, seed=1)
        self.assertGreater(n, 0)
        self.assertTrue(0.0 <= wr <= 1.0)

    def test_optimize_improves_or_keeps(self) -> None:
        w, report = learn.optimize(self.db, self.meta, games=1, rounds=1, seed=3,
                                   keys=["face"])
        self.assertIn("face", w)
        self.assertGreaterEqual(report.best, 0.0)


if __name__ == "__main__":
    unittest.main()
