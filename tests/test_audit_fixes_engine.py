"""C 组修复复现测试：斩杀口径 / 英雄技能分流 / AOE 加分 / 攻击次数 / 快照字段。

构造参考 tests/test_lethal_complete.py 的 Board / SideState / Unit。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from hsai import cards as cards_mod
from hsai import policy
from hsai.advisor import Advisor
from hsai.cards import Card, CardDB
from hsai.model import Board, SideState, Unit, parse_effect
from hsai.sim.engine import _reset_attacks
from hsai.state import replay_log_text
from hsai.state_types import Side

DB: CardDB = cards_mod.load()
FIXTURE = Path(__file__).parent / "fixtures" / "sample_game.log"


def _board(me: SideState, foe: SideState) -> Board:
    return Board([me, foe], turn=9, active=0, cards=DB)


class RandomEnemyLethalTest(unittest.TestCase):
    """缺陷 10：random_enemy 有存活随从时打随从，不该算成打脸斩杀。"""

    @staticmethod
    def _spell() -> Card:
        return Card(id="T_RND", name="随机飞弹", cost=1, type="SPELL",
                    text="对一个随机敌人造成3点伤害")

    def _with_spell(self, foe: SideState) -> dict:
        me = SideState(hero_hp=30, mana=10, max_mana=10, hand=[self._spell()])
        return policy.max_face_damage(_board(me, foe), 0, DB)

    def test_foe_has_minion_no_false_lethal(self) -> None:
        self.assertEqual(parse_effect(self._spell()).scope, "random_enemy")
        foe = SideState(hero_hp=3)
        foe.board.append(Unit("", "随从", 2, 5))
        r = self._with_spell(foe)
        self.assertEqual(r["spell"], 0)
        self.assertFalse(r["lethal"])

    def test_empty_board_counts_as_face(self) -> None:
        r = self._with_spell(SideState(hero_hp=3))
        self.assertEqual(r["spell"], 3)
        self.assertTrue(r["lethal"])


class HeroPowerTargetTest(unittest.TestCase):
    """缺陷 11：apply_action 的 hero_power 按 action.target 分流。"""

    @staticmethod
    def _board_with_minion() -> Board:
        me = SideState(hero_hp=30, mana=10, max_mana=10, hero_class="MAGE")
        foe = SideState(hero_hp=30)
        foe.board.append(Unit("", "随从", 2, 5))
        return _board(me, foe)

    def test_face_action_hits_hero_not_minion(self) -> None:
        b = self._board_with_minion()
        acts = [a for a in policy.legal_actions(b, 0, DB)
                if a.kind == "hero_power" and a.target == "enemy_hero"]
        self.assertTrue(acts)
        nb = policy.apply_action(b, 0, acts[0], DB)
        self.assertEqual(nb.sides[1].hero_hp, 29)
        self.assertEqual(nb.sides[1].board[0].hp, 5)

    def test_minion_action_hits_minion_not_hero(self) -> None:
        b = self._board_with_minion()
        acts = [a for a in policy.legal_actions(b, 0, DB)
                if a.kind == "hero_power" and a.target.startswith("enemy:")]
        self.assertTrue(acts)
        nb = policy.apply_action(b, 0, acts[0], DB)
        self.assertEqual(nb.sides[1].hero_hp, 30)
        self.assertEqual(nb.sides[1].board[0].hp, 4)

    def test_lethal_sequence_cashes_face_hero_power(self) -> None:
        b = self._board_with_minion()
        b.sides[1].hero_hp = 1
        seq = policy._lethal_sequence(b, 0, DB)
        self.assertIsNotNone(seq)
        self.assertTrue(any(a.kind == "hero_power" and a.target == "enemy_hero"
                            for a in seq))


class AoeChoiceTest(unittest.TestCase):
    """缺陷 12：enemy_minions 的 AOE 解场加分要生效。"""

    def test_enemy_minions_aoe_gets_score(self) -> None:
        card = Card(id="T_AOE", name="群体伤害", cost=4, type="SPELL",
                    text="对所有敌方随从造成2点伤害")
        eff = parse_effect(card)
        self.assertEqual(eff.scope, "enemy_minions")
        self.assertTrue(eff.aoe)
        foe = SideState(hero_hp=30)
        foe.board.append(Unit("", "小怪A", 1, 2))
        foe.board.append(Unit("", "小怪B", 2, 1))
        b = _board(SideState(hero_hp=30), foe)
        score, why = policy.score_choice(card, eff, b, 0, 10)
        self.assertIn("能解掉", why)
        self.assertGreater(score, 2.0)


class AttackResetTest(unittest.TestCase):
    """缺陷 13：每回合普通武器 1 次 / 风怒 2 次 / 无武器英雄最多 1 次。"""

    @staticmethod
    def _count(weapon: Unit | None, hero_atk: int = 0) -> int:
        me = SideState(hero_hp=30, weapon=weapon, hero_atk=hero_atk)
        _reset_attacks(me)
        board = _board(me, SideState(hero_hp=30))
        count = 0
        for _ in range(5):
            face = next((a for a in policy.legal_actions(board, 0, DB)
                         if a.kind == "hero_attack" and a.target == "enemy_hero"), None)
            if face is None:
                break
            board = policy.apply_action(board, 0, face, DB)
            count += 1
        return count

    def test_normal_weapon_once(self) -> None:
        self.assertEqual(self._count(Unit("", "武器", 3, 5)), 1)

    def test_windfury_weapon_twice(self) -> None:
        self.assertEqual(self._count(Unit("", "风怒武器", 3, 5, windfury=True)), 2)

    def test_hero_attack_without_weapon_at_most_once(self) -> None:
        self.assertEqual(self._count(None, hero_atk=3), 1)


class HeroPowerUsedSnapshotTest(unittest.TestCase):
    """缺陷 9：快照字段是 bool，读取方拿到的不是 None。"""

    def test_side_as_dict_has_bool_field(self) -> None:
        view = Side(player_id=1).as_dict()
        self.assertIn("hero_power_used", view)
        self.assertIsInstance(view["hero_power_used"], bool)

    def test_snapshot_exposes_bool(self) -> None:
        # 留牌阶段不会触发局末落盘，别动 data/ 产物
        text = "\n".join(FIXTURE.read_text(encoding="utf-8").splitlines()[:40])
        st = replay_log_text(text, DB)
        snap = Advisor(DB).snapshot(st)
        self.assertIsInstance(snap["our"]["hero_power_used"], bool)


if __name__ == "__main__":
    unittest.main()
