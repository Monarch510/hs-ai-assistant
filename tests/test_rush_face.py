"""突袭不能打脸这条口径：动作枚举、打脸伤害估算、墓地计数三处一致。

突袭（RUSH）的随从入场当回合只能攻击随从。这个限制散落在 policy 的
`legal_actions` 与 `max_face_damage` 两处，只要有一处漏了，就会出现
「建议里不能打脸、斩杀却按能打脸算」这种自相矛盾。
"""

from __future__ import annotations

import unittest

from hsai import cards as cards_mod
from hsai import model, policy, state
from hsai.model import Board, SideState, Unit


def _board(**kw) -> Board:
    db = cards_mod.load()
    b = Board([SideState(hero_hp=30, mana=10, max_mana=10), SideState(hero_hp=30)], 5, 0, cards=db)
    for key, val in kw.items():
        setattr(b.sides[0], key, val)
    return b


class RushFaceDamageTest(unittest.TestCase):
    def test_rush_only_minion_not_counted_as_face_damage(self) -> None:
        # 一只突袭刚入场（rush_only=True）的 5 攻随从，本回合只能打随从
        b = _board(board=[Unit("", "突袭兵", 5, 5, rush=True, rush_only=True, attacks_left=1)])
        res = policy.max_face_damage(b, 0, b.cards)
        self.assertEqual(res["damage"], 0, "突袭刚入场的随从不能计入打脸伤害")

    def test_normal_minion_counts(self) -> None:
        b = _board(board=[Unit("", "普通兵", 5, 5, attacks_left=1)])
        res = policy.max_face_damage(b, 0, b.cards)
        self.assertEqual(res["damage"], 5)

    def test_rush_minion_without_rush_only_can_hit_face(self) -> None:
        # 下一回合 rush_only 会被清掉，这时它能打脸，不能被误伤
        b = _board(board=[Unit("", "突袭兵", 4, 4, rush=True, rush_only=False, attacks_left=1)])
        res = policy.max_face_damage(b, 0, b.cards)
        self.assertEqual(res["damage"], 4)

    def test_legal_actions_also_hides_face_attack(self) -> None:
        b = _board(board=[Unit("", "突袭兵", 5, 5, rush=True, rush_only=True, attacks_left=1)])
        acts = policy.legal_actions(b, 0, b.cards)
        descs = [a.desc for a in acts if a.kind == "attack"]
        self.assertFalse(any("攻击英雄" in d for d in descs), "突袭当回合不该出现攻击英雄的动作")


class GraveyardCountTest(unittest.TestCase):
    def test_graveyard_count_in_view(self) -> None:
        # 面板读的是 state.Side.as_dict()（展示视图），不是引擎里的 model.SideState
        side = state.Side(player_id=1)
        side.graveyard = [{"id": "A"}, {"id": "B"}, {"id": "C"}]
        view = side.as_dict()
        self.assertEqual(view["graveyard_count"], 3, "面板要能直接读墓地数量")


class HudRowTest(unittest.TestCase):
    """面板「对战信息」那一行的组装：血甲分开、不摆墓地，奥秘/武器仍要出现。"""

    def _row(self, **kw) -> str:
        from hsai import ui as ui_mod

        side = {"health": 27, "armor": 0, "hand_count": 5, "deck_count": 18,
                "board": [{"atk": 2}, {"atk": 1}], "graveyard_count": 4}
        side.update(kw)
        return ui_mod.hud_row(side, "对手")

    def test_base_line(self) -> None:
        row = self._row()
        self.assertIn("对手 27 血", row)
        self.assertIn("场攻 3", row)
        self.assertNotIn("墓地", row, "墓地数不再摆上对战信息")
        self.assertIn("27 血 · 3 甲", self._row(armor=3), "血和甲要拆成两个数")

    def test_secrets_and_weapon(self) -> None:
        row = self._row(secrets=["S1", "S2"], weapon={"atk": 3, "durability": 2})
        self.assertIn("奥秘 2", row)
        self.assertIn("武器 3/2", row)
        self.assertIn("场攻 6", row, "武器攻击力要算进场攻")

    def test_estimated_deck_is_marked(self) -> None:
        row = self._row(deck_count=22, deck_estimated=True)
        self.assertIn("牌库 约22", row, "粗估要标出来，别当实测值")


if __name__ == "__main__":
    unittest.main()
