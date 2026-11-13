"""第五轮审计：Lead 负责的 13 项回归用例。

覆盖 m04633 报告里的 1/2/3/4/8/9/10/11/12/13/17/19/25，全部先用隔离探针
复现过旧行为，再写断言钉住修后的行为。构造尽量走真库 cards.load()，
涉及写盘的一律改道 tempfile，不碰 data/ 产物。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hsai import advisor as A
from hsai import cards, policy
from hsai.cards import Card, CardDB
from hsai.model import Board, SideState, Unit
from hsai.model_effects import spell_damage_from_card
from hsai.policy_consts import CTHUN_SKILL_NAMES
from hsai.policy_race import race_plan
from hsai.state_types import Side
import hsai.model as model_mod

from tests._ui_support import prepare

prepare()                       # 没 tkinter 装替身，必须在 import hsai.ui 之前

import hsai.ui as ui_mod        # noqa: E402
from tests.test_overlay_revamp import _Case, _label_text, _walk  # noqa: E402

REAL: CardDB = cards.load()


def _board(me: SideState, foe: SideState, db: CardDB = REAL) -> Board:
    return Board([me, foe], turn=9, active=0, cards=db)


def _spell(cid: str, name: str, cost: int, dmg: int) -> Card:
    return Card(id=cid, name=name, cost=cost, type="SPELL", text=f"造成{dmg}点伤害")


def _coin() -> Card:
    return Card(id="COIN", name="幸运币", cost=0, type="SPELL",
                text="获得一个空的法力水晶")


class SpellDamageRegexTest(unittest.TestCase):
    def test_multi_digit_spell_damage_is_read_in_full(self) -> None:
        # 旧正则 (\d) 只吃个位：真实牌 ULDA_117「法术伤害+10」被算成 1
        for text, want in (("法术伤害+10", 10), ("法术伤害+12", 12),
                           ("法术伤害+5", 5), ("法术伤害+2", 2)):
            self.assertEqual(
                spell_damage_from_card(Card(id="X", name="x", text=text)), want,
                f"{text} 应读作 {want}")


class WallBreakingTest(unittest.TestCase):
    def test_largest_swing_is_saved_for_face(self) -> None:
        # 旧逻辑把攻击段升序硬喂给嘲讽，2/2、2/2、8/8 全糊在 0/5 墙上，
        # 估算 damage=0；实战先用 8 撞墙、两只 2 打脸，其实能斩 4 血英雄。
        me = SideState()
        for atk in (2, 2, 8):
            me.board.append(Unit("", "怪", atk, atk, attacks_left=1))
        foe = SideState(hero_hp=4)
        foe.board.append(Unit("W", "墙", 0, 5, taunt=True))
        est = policy.max_face_damage(_board(me, foe), 0, REAL)
        self.assertEqual(est["attack"], 12)
        self.assertEqual(est["damage"], 4)
        self.assertTrue(est["lethal"])


class LethalPrefaceTest(unittest.TestCase):
    def test_coin_preface_keeps_lethal_sequence(self) -> None:
        # 3 费 + 幸运币(0,+1) + 火球(4,6) 对 6 血：理论能斩，
        # 但贪心先选「当步有 1 点伤害」的法师技能就把火球挤掉。加费牌要先打。
        me = SideState(hero_hp=30, mana=3, max_mana=10, hero_class="MAGE",
                       hand=[_coin(), _spell("FB", "火球", 4, 6)])
        foe = SideState(hero_hp=6)
        b = _board(me, foe)
        seq = policy._lethal_sequence(b, 0, REAL)
        self.assertIsNotNone(seq, "有斩杀线路就不该返回 None")
        self.assertGreaterEqual(len(seq), 2)
        self.assertEqual(seq[0].kind, "play")
        self.assertEqual(seq[0].cost, 0, "第一步应是 0 费的加费铺垫牌")
        res = policy.plan_turn(b, REAL)
        self.assertTrue(res["lethal"]["lethal"])


class GazeShieldTest(unittest.TestCase):
    def test_divine_shield_grants_no_lifesteal(self) -> None:
        # 凝视打圣盾：整段伤害被盾吃掉，不该按 target.hp 猜一个 5 点回血。
        me = SideState(hero_hp=10)
        foe = SideState()
        target = Unit("W", "墙", 8, 8, divine=True)
        foe.board.append(target)
        b = _board(me, foe)
        policy.apply_cthun_skill(b, 0, "克苏恩的凝视", tripled=False)
        self.assertEqual(me.hero_hp, 10, "0 伤害就不该回血")
        self.assertFalse(target.divine, "盾被打掉了")


class DeathrattleHealCapTest(unittest.TestCase):
    def test_deathrattle_heal_is_capped_at_30(self) -> None:
        dr = Card(id="DR", name="亡", cost=1, type="MINION",
                  text="亡语：恢复5点生命值")
        db9 = CardDB([dr])
        for start, want in ((20, 25), (28, 30), (30, 30)):
            foe = SideState(hero_hp=start)
            foe.board.append(Unit("DR", "亡", 1, 1))
            b = Board([SideState(), foe], turn=9, active=0, cards=db9)
            b.damage_unit(foe, foe.board[0], 10)
            self.assertEqual(foe.hero_hp, want, f"{start} + 5 应封顶到 {want}")


class CthunConsistencyTest(unittest.TestCase):
    def test_used_skills_are_not_offered_again(self) -> None:
        # 同一只克苏恩的进度挂到 _cthun_by_unit 后，legal_actions 不该再列已用技能。
        me = SideState(cthun_uses_left=2)
        me.cthun_used = set(CTHUN_SKILL_NAMES)
        me._cthun_by_unit = {"CTHUN#0": set(CTHUN_SKILL_NAMES)}
        me.board.append(Unit("CTHUN", "克苏恩", 6, 6))
        acts = policy.legal_actions(_board(me, SideState()), 0, REAL)
        cthun_acts = [a for a in acts if a.kind == "cthun_skill"]
        self.assertEqual(len(cthun_acts), 0, "四个技能都用过了，不该再给选项")

    def test_play_branch_uses_per_unit_progress_for_tripled(self) -> None:
        # 全局只用过 2 个（不四倍），但当前这只已用过 3 个 —— 打出触发牌
        # 应按「同一只」判四倍，而不是按全局。旧代码这里读全局 cthun_used。
        me = SideState(cthun_uses_left=2)
        me.cthun_used = {"a", "b"}
        me._cthun_by_unit = {"default": {"a", "b", "c"}}
        me.board.append(Unit("", "友军", 1, 1))
        me.hand = [Card(id="T", name="触发", cost=1, type="SPELL", text="")]
        b = _board(me, SideState())
        nb = policy.apply_action(
            b, 0,
            policy.Action("play", card_idx=0, cost=1, target="cthun:克苏恩的愤怒"),
            REAL)
        self.assertTrue(any("24 点伤害" in line for line in nb.log),
                        "同一只已用 3 个，这次该是四倍 24 点")


class AdvisorBoardInjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        saved = model_mod._DB
        self.addCleanup(lambda: setattr(model_mod, "_DB", saved))

    @staticmethod
    def _fake_state(ours: Side) -> object:
        class St:
            cards = REAL
            turn = 9
            our_id = 1
            opp_id = 2
            deck_size = 0
            cthun_used = {"克苏恩的愤怒"}
            our_minions_played = 0
            entities: dict = {}

            def sides(self):
                return ours, Side(player_id=2)

            def effective_turn_owner(self):
                return 1

            def hero(self, pid):
                return None

        return St()

    def test_hero_power_exhausted_marks_used(self) -> None:
        # _card_view 里没有 used 键，只有 exhausted；旧代码读 used 恒 False。
        ours = Side(player_id=1, class_en="MAGE",
                    hero_power={"id": "HP1", "exhausted": True})
        me = A.board_from_state(self._fake_state(ours)).sides[0]
        self.assertTrue(me.hero_power_used)

    def test_cthun_progress_is_injected_per_unit(self) -> None:
        # 帝国之眼在场时要把全局进度挂到当前那只克苏恩名下，去重才生效。
        ours = Side(player_id=1, class_en="MAGE",
                    board=[{"id": "EYE", "name": "帝国之眼", "health": 1},
                           {"id": "CTHUN", "name": "克苏恩", "health": 6}],
                    hero_power={"id": "HP1"})
        me = A.board_from_state(self._fake_state(ours)).sides[0]
        self.assertEqual(me.cthun_uses_left, 2)
        self.assertEqual(me._cthun_by_unit, {"CTHUN#1": {"克苏恩的愤怒"}})


class RaceHeroPowerTest(unittest.TestCase):
    def test_mage_target_power_counts_as_burst(self) -> None:
        # 法师技能 scope="target" 旧白名单 ("enemy_hero","enemy") 会漏掉这 1 点。
        hp = Card(id="HP_MAGE", name="火焰冲击", cost=2, type="HERO_POWER",
                  text="造成1点伤害")
        me = SideState(hero_hp=30, hero_class="MAGE", hero_power=hp)
        res = race_plan(_board(me, SideState(hero_hp=30)), 0, REAL)
        self.assertEqual(res["my_damage"], 1.0)


class KnapsackTest(unittest.TestCase):
    def test_picks_best_subset_within_mana(self) -> None:
        # 8 张 1 费法术伤害 1..8，3 费该挑 8+7+6，不是逐张贪心。
        spells = [_spell(f"S{i}", f"法术{i}", 1, i) for i in range(1, 9)]
        me = SideState(hero_hp=30, mana=3, max_mana=10, hand=spells)
        est = policy.max_face_damage(_board(me, SideState(hero_hp=30)), 0, REAL)
        self.assertEqual(est["spell"], 21)

    def test_many_face_spells_stay_fast(self) -> None:
        # 旧子集穷举 2^n：22 张法术要 7 秒。背包 DP 应远小于 1 秒。
        spells = [_spell(f"S{i}", f"法术{i}", 1, 1) for i in range(22)]
        me = SideState(hero_hp=30, mana=10, max_mana=10, hand=spells)
        t0 = time.monotonic()
        est = policy.max_face_damage(_board(me, SideState(hero_hp=30)), 0, REAL)
        elapsed = time.monotonic() - t0
        self.assertEqual(est["spell"], 10)
        self.assertLess(elapsed, 1.0, "22 张手牌不该再指数爆炸")


class DeckRowWrapTest(_Case):
    def test_deck_row_name_has_wraplength(self) -> None:
        # 加了类型/功能标签后长牌名要换行，不能被右侧百分比挤成裁字。
        snap = self._deck_snap(12)
        self.ov._collapsed.discard("牌库剩余")
        self.ov._render(snap)
        names = [w for w in _walk(self.ov._deck_list)
                 if "第1张牌" in _label_text(w)]
        self.assertTrue(names, "要画出牌库明细行")
        self.assertEqual(names[0].cget("wraplength"), self.ov.width - 70)


class JsonlSplitTest(unittest.TestCase):
    def test_unicode_line_separator_keeps_whole_row(self) -> None:
        # splitlines 会按 U+2028 切；一行牌名里带了它就被切成两截、整行被吞。
        with tempfile.TemporaryDirectory(prefix="hsai-r5-") as td:
            p = Path(td) / "obs.jsonl"
            note = "x" + chr(0x2028) + "y"
            p.write_text(json.dumps({"note": note}, ensure_ascii=False) + "\n",
                         encoding="utf-8")
            rows = A.load_observations(p)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["note"], note)


class RecordResultSaveTest(unittest.TestCase):
    @staticmethod
    def _adv() -> A.Advisor:
        adv = object.__new__(A.Advisor)
        adv.predictor = mock.Mock()
        adv.predictor.retrospect.return_value = mock.Mock(
            trusted=False, name="x", archetype_id=None, unexplained=[])
        adv._trace = []
        adv.cards = REAL
        adv.meta = mock.Mock()
        adv.learned_count = 0
        return adv

    @staticmethod
    def _st() -> mock.Mock:
        ours = Side(player_id=1, class_en="MAGE")
        opp = Side(player_id=2, class_en="MAGE")
        st = mock.Mock()
        st.sides = mock.Mock(return_value=(ours, opp))
        st.revealed_opp_order = []
        return st

    def _run(self, added: int) -> int:
        with tempfile.TemporaryDirectory(prefix="hsai-r5-") as td:
            tmp = Path(td)
            with mock.patch.object(A, "DATA_DIR", tmp), \
                 mock.patch.object(A, "OBSERVATIONS_PATH", tmp / "obs.jsonl"), \
                 mock.patch.object(A, "observation_row", return_value={"schema": 2}), \
                 mock.patch.object(A, "DeckPredictor", return_value=mock.Mock()), \
                 mock.patch("hsai.learn.absorb_observations", return_value=added):
                adv = self._adv()
                adv._record_result(self._st(), mock.Mock())
                return adv.meta.save.call_count

    def test_no_new_archetype_does_not_save(self) -> None:
        self.assertEqual(self._run(0), 0, "没长出新原型就别每局白写一遍")

    def test_new_archetype_saves_once(self) -> None:
        self.assertEqual(self._run(3), 1)


class BatStartupTest(unittest.TestCase):
    def test_pyw_uses_valid_launcher(self) -> None:
        # py -3w 在只装 py -3 的机器上不存在，会静默启动失败还 exit 0。
        bat = Path(__file__).resolve().parent.parent / "启动AI助手.bat"
        data = bat.read_bytes()
        self.assertIn(b'set "PYW=pyw -3"', data)
        self.assertNotIn(b"py -3w", data)


if __name__ == "__main__":
    unittest.main()
