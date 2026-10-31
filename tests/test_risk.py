"""风险概率的口径测试：必须跟着「对手这回合的费用」变化。

背景：`aoe_risk` / `removal_risk` 曾经只算「他手上有没有」，不看费用，
导致对面 1 费回合也报「清场 50%」，进而把「别铺满」的防守建议推到每回合。
"""

from __future__ import annotations

import unittest

from hsai import cards, infer, meta


class RiskManaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards.load()
        cls.predictor = infer.DeckPredictor(meta.MetaDB.load(cls.db), cls.db)

    def _risk(self, mana: int) -> tuple[float, float]:
        pred = self.predictor.predict(
            "WARLOCK", [], turn=6, hand_count=5, deck_count=18, opp_mana=mana
        )
        return pred.aoe_risk, pred.removal_risk

    def test_aoe_risk_grows_with_mana(self) -> None:
        low, _ = self._risk(1)
        high, _ = self._risk(10)
        self.assertLess(low, high, "低费回合的清场概率必须低于高费回合")

    def test_one_mana_cannot_have_expensive_board_clear(self) -> None:
        aoe, _ = self._risk(1)
        self.assertEqual(aoe, 0.0, "1 费回合不可能打出任意费用的清场")


if __name__ == "__main__":
    unittest.main()
