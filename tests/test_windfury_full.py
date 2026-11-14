"""风怒 / 超级风怒全链路：静态卡面、动态赋予、4 段攻击、无法攻击英雄。

真实卡库里「超级风怒」只写在卡面文本，mechanics 里连普通 WINDFURY 都没有，
所以识别必须读文本；日志动态赋予走 tag，不能只看卡面。
"""
from __future__ import annotations

import unittest

from hsai import cards as cards_mod
from hsai import policy, powerlog
from hsai.advisor import board_from_state
from hsai.cards import MECHANIC_CN, Card, CardDB, cannot_attack_hero, windfury_attacks
from hsai.model import Board, SideState, Unit
from hsai.state import ZONE_PLAY, Entity, GameState
from hsai.sim.engine import _reset_attacks

REAL: CardDB = cards_mod.load()


def _card(cid: str, name: str, *, mechanics=(), text: str = "",
          card_type: str = "MINION", atk: int = 3, durability: int = 4) -> Card:
    return Card(id=cid, name=name, cost=1, type=card_type, atk=atk,
                health=3, durability=durability, mechanics=tuple(mechanics), text=text)


def _board(me: SideState, foe: SideState | None = None, db: CardDB = REAL) -> Board:
    return Board([me, foe or SideState(hero_hp=30)], turn=9, active=0, cards=db)


class CardRecognitionTest(unittest.TestCase):
    def test_attacks_per_turn_from_mechanics_and_text(self) -> None:
        self.assertEqual(windfury_attacks(_card("A", "普通")), 1)
        self.assertEqual(windfury_attacks(_card("B", "风怒", mechanics=("WINDFURY",))), 2)
        self.assertEqual(windfury_attacks(_card("C", "超级风怒", text="<b>超级风怒</b>")), 4)
        self.assertEqual(windfury_attacks(_card("D", "机制超级", mechanics=("MEGA_WINDFURY",))), 4)
        self.assertEqual(windfury_attacks(None), 1)

    def test_real_mega_windfury_cards_recognized(self) -> None:
        for cid in ("DRGA_005", "BOM_05_Anetheron_07t", "GVG_111t",
                    "WW_382", "DRGA_24w", "LOOTA_821"):
            with self.subTest(cid=cid):
                self.assertEqual(windfury_attacks(REAL.get(cid)), 4)
        self.assertEqual(windfury_attacks(REAL.get("CS2_106")), 1)

    def test_mega_mechanic_has_chinese_keyword(self) -> None:
        self.assertEqual(MECHANIC_CN["MEGA_WINDFURY"], "超级风怒")
        c = _card("K", "机制牌", mechanics=("MEGA_WINDFURY",))
        self.assertIn("超级风怒", c.keyword_cn)

    def test_cannot_attack_hero_only_for_self_limited_cards(self) -> None:
        self.assertTrue(cannot_attack_hero(REAL.get("DRGA_24w")))
        self.assertTrue(cannot_attack_hero(REAL.get("AT_125")))
        # 战吼把限制赋予「该随从」，牌自身不受限
        self.assertFalse(cannot_attack_hero(REAL.get("CATA_564")))
        self.assertFalse(cannot_attack_hero(_card("X", "普通")))
        self.assertFalse(cannot_attack_hero(None))


class UnitMaxAttacksTest(unittest.TestCase):
    def test_unit_max_attacks(self) -> None:
        self.assertEqual(Unit("", "普通", 3, 3).max_attacks, 1)
        self.assertEqual(Unit("", "风怒", 3, 3, windfury=True).max_attacks, 2)
        self.assertEqual(Unit("", "超级", 3, 3, mega_windfury=True).max_attacks, 4)
        self.assertEqual(SideState(board=[Unit("", "超级", 3, 3, mega_windfury=True)]).board_attack, 12)

    def test_clone_keeps_new_fields(self) -> None:
        u = Unit("", "超级", 3, 3, mega_windfury=True, cannot_attack_hero=True)
        c = u.clone()
        self.assertTrue(c.mega_windfury)
        self.assertTrue(c.cannot_attack_hero)
        self.assertEqual(c.max_attacks, 4)


class StaticPlayTest(unittest.TestCase):
    def test_playing_mega_windfury_charge_minion_has_four_attacks(self) -> None:
        mega = _card("MEGA_M", "超级冲锋", mechanics=("CHARGE",), text="<b>超级风怒</b>")
        db = CardDB([mega])
        me = SideState(hero_hp=30, mana=10, max_mana=10, hand=[mega])
        nb = policy.apply_action(_board(me, db=db), 0,
                                 policy.Action("play", card_idx=0, cost=1), db)
        u = nb.sides[0].board[0]
        self.assertTrue(u.mega_windfury)
        self.assertEqual(u.max_attacks, 4)
        self.assertEqual(u.attacks_left, 4)

    def test_playing_mega_windfury_weapon(self) -> None:
        w = _card("MEGA_W", "超级风怒刀", card_type="WEAPON", text="<b>超级风怒</b>")
        db = CardDB([w])
        me = SideState(hero_hp=30, mana=10, max_mana=10, hand=[w])
        nb = policy.apply_action(_board(me, db=db), 0,
                                 policy.Action("play", card_idx=0, cost=1), db)
        self.assertEqual(nb.sides[0].weapon.max_attacks, 4)


class FourSwingLethalTest(unittest.TestCase):
    def test_mega_windfury_minion_swings_four_times(self) -> None:
        me = SideState(hero_hp=30, board=[Unit("", "超级", 3, 3, mega_windfury=True, attacks_left=4)])
        est = policy.max_face_damage(_board(me, SideState(hero_hp=12)), 0, REAL)
        self.assertEqual(est["attack"], 12)
        self.assertEqual(est["damage"], 12)
        self.assertTrue(est["lethal"])

    def test_normal_windfury_still_two(self) -> None:
        me = SideState(hero_hp=30, board=[Unit("", "风怒", 3, 3, windfury=True, attacks_left=2)])
        self.assertEqual(policy.max_face_damage(_board(me), 0, REAL)["attack"], 6)

    def test_mega_windfury_hero_swings_four_times(self) -> None:
        me = SideState(hero_hp=30, hero_atk=3, hero_attacks_left=4)
        self.assertEqual(policy.max_face_damage(_board(me), 0, REAL)["attack"], 12)

    def test_sim_reset_uses_max_attacks(self) -> None:
        side = SideState(hero_hp=30, weapon=Unit("", "超级刀", 3, 4, mega_windfury=True))
        _reset_attacks(side)
        self.assertEqual(side.hero_attacks_left, 4)
        u = Unit("", "超级随从", 3, 3, mega_windfury=True)
        side.board.append(u)
        u.attacks_left = u.max_attacks
        self.assertEqual(u.attacks_left, 4)


class CannotAttackHeroTest(unittest.TestCase):
    def test_minion_cannot_attack_hero_in_estimate_and_actions(self) -> None:
        me = SideState(hero_hp=30, board=[Unit("", "受限", 3, 3,
                                               cannot_attack_hero=True, attacks_left=1)])
        self.assertEqual(policy.max_face_damage(_board(me), 0, REAL)["damage"], 0)
        descs = [a.desc for a in policy.legal_actions(_board(me), 0, REAL)
                 if a.kind == "attack"]
        self.assertFalse(any("攻击英雄" in d for d in descs))

    def test_weapon_cannot_attack_hero_hides_face_swing(self) -> None:
        me = SideState(hero_hp=30, hero_atk=3, hero_attacks_left=4,
                       weapon=Unit("", "受限刀", 3, 4, cannot_attack_hero=True))
        self.assertEqual(policy.max_face_damage(_board(me), 0, REAL)["damage"], 0)
        descs = [a.desc for a in policy.legal_actions(_board(me), 0, REAL)
                 if a.kind == "hero_attack"]
        self.assertFalse(any("攻击英雄" in d for d in descs))

    def test_played_claw_is_limited(self) -> None:
        w = _card("CLAW", "迦拉克隆之爪", card_type="WEAPON",
                  text="<b>超级风怒</b>\n无法攻击英雄。")
        db = CardDB([w])
        me = SideState(hero_hp=30, mana=10, max_mana=10, hand=[w])
        nb = policy.apply_action(_board(me, db=db), 0,
                                 policy.Action("play", card_idx=0, cost=1), db)
        self.assertTrue(nb.sides[0].weapon.cannot_attack_hero)
        self.assertEqual(nb.sides[0].weapon.max_attacks, 4)


class DynamicTagTest(unittest.TestCase):
    def test_tag_whitelist_accepts_mega_windfury(self) -> None:
        self.assertEqual(powerlog.tag_key("MEGA_WINDFURY"), "MEGA_WINDFURY")
        self.assertEqual(powerlog.tag_key("WINDFURY"), "WINDFURY")

    def _state_with_dynamic(self, tag: str) -> GameState:
        plain = Card(id="PLAIN", name="普通随从", cost=1, type="MINION", atk=2, health=2)
        db = CardDB([plain])
        st = GameState(db)
        st.turn, st.turn_owner, st.our_id, st.opp_id = 3, 1, 1, 2
        st.player_ids = {1: 10}
        st.entities[10] = Entity(10, tags={"PLAYER_ID": 1, "HERO_ENTITY": 20})
        st.entities[20] = Entity(20, card_id="HERO_01", controller=1, zone=ZONE_PLAY,
                                 tags={"ATK": 0, "HEALTH": 30})
        st.entities[30] = Entity(30, card_id="PLAIN", controller=1, zone=ZONE_PLAY,
                                 tags={"ATK": 2, "HEALTH": 2, tag: 1})
        return st

    def test_dynamic_mega_windfury_reaches_board(self) -> None:
        st = self._state_with_dynamic("MEGA_WINDFURY")
        self.assertEqual(st._card_view(st.entities[30])["max_attacks"], 4)
        u = board_from_state(st).sides[0].board[0]
        self.assertEqual(u.max_attacks, 4)
        self.assertEqual(u.attacks_left, 4)

    def test_dynamic_plain_windfury_reaches_board(self) -> None:
        st = self._state_with_dynamic("WINDFURY")
        self.assertEqual(st._card_view(st.entities[30])["max_attacks"], 2)
        u = board_from_state(st).sides[0].board[0]
        self.assertEqual(u.max_attacks, 2)

    def test_can_attack_respects_dynamic_limit(self) -> None:
        st = self._state_with_dynamic("MEGA_WINDFURY")
        e = st.entities[30]
        # 打过 2 次后仍应能再打（上限 4），普通随从第 1 次就不行
        e.tags["NUM_ATTACKS_THIS_TURN"] = 2
        self.assertTrue(st.can_attack(e))


if __name__ == "__main__":
    unittest.main()
