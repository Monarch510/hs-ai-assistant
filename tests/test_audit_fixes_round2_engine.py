"""C 组第二轮复核测试：uid 认同一只 / 解场识别 / 圣盾口径 / 位置构造未打乱。

构造参考 tests/test_lethal_complete.py 与 tests/test_audit_fixes_engine.py。
"""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from hsai import cards as cards_mod
from hsai import model, policy
from hsai.cards import Card, CardDB
from hsai.model import Board, SideState, Unit

DB: CardDB = cards_mod.load()
REPO = Path(__file__).resolve().parent.parent
EVAL_SRC = REPO / "hsai" / "model_eval.py"

# 把权重清零只留 taunt 一项：evaluate 的返回值就只剩「墙的阻滞分」，
# 能直接和 max_face_damage 的 taunt_block 对账，不被圣盾自身的估值干扰。
TAUNT_ONLY = {
    "tempo": 0.0, "face": 0.0, "hand": 0.0, "health": 0.0,
    "taunt": 1.0, "aoe_risk": 0.0, "fatigue": 0.0,
}


def _board(me: SideState, foe: SideState) -> Board:
    return Board([me, foe], turn=9, active=0, cards=DB)


def _two_twins() -> Board:
    """对方场面两只同 card_id 随从：旧 card_id 兜底会把两只都判成「没死」。"""
    me = SideState(hero_hp=30, mana=10, max_mana=10, hero_class="MAGE")
    me.board.append(Unit("", "我方大怪", 3, 3, attacks_left=1))
    foe = SideState(hero_hp=30)
    foe.board.append(Unit("TWIN", "双子A", 2, 2))
    foe.board.append(Unit("TWIN", "双子B", 2, 2))
    return _board(me, foe)


class UidContractTest(unittest.TestCase):
    """① uid 契约：clone 后靠 uid 认同一只，同 card_id 不能混。"""

    def test_same_card_id_units_have_distinct_uid(self) -> None:
        a = Unit("TWIN", "双子A", 2, 2)
        b = Unit("TWIN", "双子B", 2, 2)
        self.assertEqual(a.card_id, b.card_id)
        self.assertNotEqual(a.uid, b.uid)

    def test_clone_preserves_uid_of_each_unit(self) -> None:
        before = _two_twins()
        after = before.clone()
        for i, (u0, u1) in enumerate(zip(before.sides[1].board, after.sides[1].board)):
            with self.subTest(i=i):
                self.assertIsNot(u0, u1)          # 确实是副本，不是同一对象
                self.assertEqual(u0.uid, u1.uid)  # 但仍是同一只
        self.assertNotEqual(after.sides[1].board[0].uid, after.sides[1].board[1].uid)

    def test_dataclasses_replace_preserves_uid(self) -> None:
        u = Unit("TWIN", "双子", 2, 2)
        damaged = dataclasses.replace(u, hp=1)
        self.assertEqual(damaged.uid, u.uid)
        self.assertIsNot(damaged, u)

    def test_uid_appended_does_not_break_positional_constructor(self) -> None:
        # 全仓大量 Unit("", "名", atk, hp, ...) 位置构造，新字段必须在末尾且带默认
        u = Unit("", "随从", 4, 5)
        self.assertEqual((u.card_id, u.name, u.atk, u.hp), ("", "随从", 4, 5))
        self.assertTrue(u.alive)
        self.assertIsInstance(u.uid, int)


class ClearRecognitionTest(unittest.TestCase):
    """② C2 核心 bug：打死两只同 card_id 里的一只，必须识别成「死了一只」。"""

    def test_attack_one_twin_yields_free_trade(self) -> None:
        # 3/3 撞 2/2：对面死一只、我方活 → free_trade（6 分）。
        # 旧 card_id 兜底 killed=0 会落到 attack 的 "board"（3 分），解场加分丢失。
        before = _two_twins()
        act = policy.Action("attack", unit_idx=0, target="enemy:0")
        after = policy.apply_action(before, 0, act, DB)
        self.assertEqual(len(after.sides[1].board), 1)
        self.assertEqual(after.sides[1].board[0].uid, before.sides[1].board[1].uid)
        self.assertEqual(policy.categorize(act, before, after, 0), "free_trade")

    def test_hero_power_kill_one_twin_yields_free_trade(self) -> None:
        # 法师技能点掉 1 血那只：旧口径 killed=0 会落到 "defend"，
        # 新口径识别到死了一只 → free_trade，信号最干净。
        before = _two_twins()
        before.sides[1].board[0].hp = 1
        act = policy.Action("hero_power", cost=2, target="enemy:0")
        after = policy.apply_action(before, 0, act, DB)
        self.assertEqual(len(after.sides[1].board), 1)
        self.assertEqual(policy.categorize(act, before, after, 0), "free_trade")

    def test_old_card_id_fallback_misses_both_twins(self) -> None:
        # 反证旧 bug 的机制：死了一只后另一只同 id 还在，card_id 口径把两只都判没死。
        before = _two_twins()
        act = policy.Action("attack", unit_idx=0, target="enemy:0")
        after = policy.apply_action(before, 0, act, DB)
        foe0, foe1 = before.sides[1].board, after.sides[1].board
        old_killed = sum(1 for u in foe0 if u.alive
                         and not any(x.card_id == u.card_id for x in foe1))
        uid_killed = sum(1 for u in foe0 if u.alive
                         and not any(x.uid == u.uid for x in foe1))
        self.assertEqual(old_killed, 0)   # 旧口径漏判
        self.assertEqual(uid_killed, 1)   # uid 口径识别到

    def test_reason_names_only_the_killed_twin(self) -> None:
        before = _two_twins()
        act = policy.Action("attack", unit_idx=0, target="enemy:0")
        after = policy.apply_action(before, 0, act, DB)
        reason = policy._reason(before, after, act, 0)
        self.assertIn("解掉 双子A", reason)
        self.assertNotIn("双子B", reason)  # 活着的那只不会被当成死/残

    def test_apply_effect_destroy_one_twin_is_recognized(self) -> None:
        # 走 model.apply_effect 的 target 消灭分支，不依赖 apply_action
        b = _two_twins()
        eff = model.parse_effect(Card(id="T_KILL", name="点杀", cost=1, type="SPELL",
                                      text="消灭一个随从"))
        self.assertTrue(eff.destroy)
        victim_uid = b.sides[1].board[0].uid
        survivor_uid = b.sides[1].board[1].uid
        b.apply_effect(eff, b.sides[0], b.sides[1].board[0])
        self.assertEqual(len(b.sides[1].board), 1)
        self.assertNotIn(victim_uid, [x.uid for x in b.sides[1].board])
        self.assertEqual(b.sides[1].board[0].uid, survivor_uid)  # 活着的仍是原来另一只


class DivineTauntParityTest(unittest.TestCase):
    """③ C1：圣盾嘲讽按 3 点计，与 policy_lethal 的 taunt_block 同口径。"""

    @staticmethod
    def _with_taunt(divine: bool, hp: int = 8) -> Board:
        me = SideState(hero_hp=30, mana=10, max_mana=10)
        me.board.append(Unit("", "大哥", 5, 5, attacks_left=1))
        foe = SideState(hero_hp=30)
        foe.board.append(Unit("T_WALL", "墙", 0, hp, taunt=True, divine=divine))
        return _board(me, foe)

    def test_source_no_longer_has_identity_expression(self) -> None:
        src = EVAL_SRC.read_text(encoding="utf-8")
        self.assertNotIn("(0 if u.divine else 0)", src)

    def test_taunt_block_uses_3_for_divine(self) -> None:
        r = policy.max_face_damage(self._with_taunt(divine=True), 0, DB)
        self.assertEqual(r["taunt_block"], 3)

    def test_evaluate_penalizes_divine_wall_less_than_plain_hp(self) -> None:
        # 清零权重后 evaluate 只剩墙的阻滞分：圣盾按 3 计 → 墙更薄 → 扣分更少。
        # 旧恒等表达式 u.hp + 0 会让两者同为 8，分数相等，这个断言就会失败。
        plain = model.evaluate(self._with_taunt(False, 8), TAUNT_ONLY, 0.0, 0)
        divine = model.evaluate(self._with_taunt(True, 8), TAUNT_ONLY, 0.0, 0)
        self.assertAlmostEqual(plain, -0.4 * (min(5, 8) * 0.6), places=6)
        self.assertAlmostEqual(divine, -0.4 * (min(5, 3) * 0.6), places=6)
        self.assertGreater(divine, plain)

    def test_evaluate_taunt_penalty_matches_lethal_taunt_block(self) -> None:
        # 同一盘面：evaluate 的阻滞分与 max_face_damage 的 taunt_block 同口径。
        for divine in (False, True):
            for hp in (2, 8):
                with self.subTest(divine=divine, hp=hp):
                    b = self._with_taunt(divine, hp)
                    block = policy.max_face_damage(b, 0, DB)["taunt_block"]
                    reach = b.sides[0].board_attack
                    expected = -0.4 * (min(reach, block) * 0.6)
                    self.assertAlmostEqual(
                        model.evaluate(b, TAUNT_ONLY, 0.0, 0), expected, places=6)


class RegressionSmokeTest(unittest.TestCase):
    """④ 回归：uid 改动没动到 categorize 的其它分支 / 斩杀估算。"""

    def test_lethal_branch_still_wins(self) -> None:
        b = _two_twins()
        b.sides[1].hero_hp = 1
        act = policy.Action("attack", unit_idx=0, target="enemy_hero")
        after = policy.apply_action(b, 0, act, DB)
        self.assertEqual(policy.categorize(act, b, after, 0), "lethal")

    def test_no_kill_attack_still_board(self) -> None:
        b = _two_twins()
        b.sides[1].board[0].hp = 9
        b.sides[1].board[1].hp = 9
        act = policy.Action("attack", unit_idx=0, target="enemy:0")
        after = policy.apply_action(b, 0, act, DB)
        self.assertEqual(policy.categorize(act, b, after, 0), "board")

    def test_max_face_damage_still_reports_lethal(self) -> None:
        b = _two_twins()
        b.sides[1].hero_hp = 3
        self.assertTrue(policy.max_face_damage(b, 0, DB)["lethal"])


if __name__ == "__main__":
    unittest.main()
