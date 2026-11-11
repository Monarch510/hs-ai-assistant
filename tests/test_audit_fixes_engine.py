"""审计修复回归用例（引擎 / 数据 / 启动降级 / UI 契约）。

原来按审计轮次散在 engine / round2_engine / round2_data / startup /
round2_ui 五个文件，都是纯顶层 import、不需要 tkinter 替身；按这个体制
合到这里，方法与契约逐字保留。startup 的离线空库单独叫 EMPTY_DB，免得
和引擎用例用的真卡库 DB 混。
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from hsai import (
    advisor, ai, cards, decks as decks_mod, logutil, logwatch, memory, meta,
    model, policy, powerlog as pl, review, ui, ui_state, ui_theme,
)
from hsai.advisor import Advisor, board_from_state
from hsai.cards import Card, CardDB
from hsai.cli_setup_log import cmd_setup_log
from hsai.decks import SavedDeck
from hsai.live import ReplaySource
from hsai.model import Board, SideState, Unit, parse_effect
from hsai.sim.engine import _reset_attacks
from hsai.state import ZONE_PLAY, Entity, GameState, replay_log_text
from hsai.state_types import Side
from hsai.ui_ai import AiWorkers


DB: CardDB = cards.load()
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

    def test_attack_reset_counts(self) -> None:
        for name, weapon, hero_atk, expected in (
            ("普通武器", Unit("", "武器", 3, 5), 0, 1),
            ("风怒武器", Unit("", "风怒武器", 3, 5, windfury=True), 0, 2),
            ("无武器英雄", None, 3, 1),
        ):
            with self.subTest(name=name):
                self.assertEqual(self._count(weapon, hero_atk), expected)


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




# 把权重清零只留 taunt 一项：evaluate 的返回值就只剩「墙的阻滞分」，
# 能直接和 max_face_damage 的 taunt_block 对账，不被圣盾自身的估值干扰。
TAUNT_ONLY = {
    "tempo": 0.0, "face": 0.0, "hand": 0.0, "health": 0.0,
    "taunt": 1.0, "aoe_risk": 0.0, "fatigue": 0.0,
}


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




class SideAsDictTest(unittest.TestCase):
    """G2：快照 Side 键集合与墓地只给数量，别把整份列表塞进去。"""

    EXPECTED = {
        "class_cn", "class_en", "health", "armor", "mana", "max_mana",
        "hand", "hand_count", "board", "deck_count", "deck_estimated",
        "secrets", "quests", "ongoing", "weapon", "hero_power",
        "hero_power_used", "graveyard_count",
    }

    def test_keys_and_defaults(self) -> None:
        d = Side(player_id=1).as_dict()
        self.assertEqual(set(d), self.EXPECTED)
        self.assertFalse(d["hero_power_used"])
        self.assertNotIn("graveyard", d)

    def test_graveyard_only_count(self) -> None:
        s = Side(player_id=1, graveyard=[{"id": "a"}, {"id": "b"}, {"id": "c"}])
        d = s.as_dict()
        self.assertEqual(d["graveyard_count"], 3)
        self.assertNotIn("graveyard", d)


class BuildPromptHeroPowerTest(unittest.TestCase):
    """G2：英雄技能明确区分「可用 / 已用」，且整段 prompt 不漏出 None。"""

    def _snap(self, used: bool) -> dict:
        return {
            "turn": 1,
            "is_our_turn": True,
            "our": {
                "class_cn": "法师", "health": 30, "armor": 0,
                "mana": 1, "max_mana": 1, "deck_count": 25,
                "hand": [], "board": [],
                "hero_power": {"name": "火焰冲击", "cost": 2, "text": "造成1点伤害"},
                "hero_power_used": used,
            },
            "opp": {
                "class_cn": "战士", "health": 30, "armor": 0,
                "hand_count": 3, "deck_count": 26, "board": [],
            },
        }

    def test_available_used_and_no_none(self) -> None:
        free = ai.build_prompt(self._snap(False))
        used = ai.build_prompt(self._snap(True))
        self.assertIn("可用", free)
        self.assertIn("已用", used)
        self.assertNotIn("None", free)
        self.assertNotIn("None", used)


class TagKeyFilterTest(unittest.TestCase):
    """B1：KNOWN_TAGS 是白名单，未知名字/数字一律过滤掉。"""

    def test_known_and_unknown(self) -> None:
        self.assertEqual(pl.tag_key("ZONE"), "ZONE")
        for tag in ("STEP", "NUM_CARDS_DRAWN_THIS_TURN", "FATIGUE", "WINDFURY"):
            self.assertIsNotNone(pl.tag_key(tag), f"{tag} 必须放行")
        self.assertIsNone(pl.tag_key("TOTALLY_UNKNOWN_TAG"))
        self.assertIsNone(pl.tag_key("999999"))

    def test_parse_text_drops_unknown_tag(self) -> None:
        text = "\n".join([
            "D 10:00:00.0000000 GameState.DebugPrintPower() - "
            "TAG_CHANGE Entity=6 tag=TOTALLY_UNKNOWN_TAG value=1",
            "D 10:00:00.0000001 GameState.DebugPrintPower() - "
            "TAG_CHANGE Entity=6 tag=ZONE value=HAND",
        ])
        events = pl.parse_text(text)
        self.assertFalse(any(
            ev.kind == "tag_change" and ev.data.get("tag") == "TOTALLY_UNKNOWN_TAG"
            for ev in events
        ))
        self.assertTrue(any(
            ev.kind == "tag_change" and ev.data.get("tag") == "ZONE"
            for ev in events
        ))


class AtomicTruncateTest(unittest.TestCase):
    """B2：截断走 tmp + os.replace，不留半截 jsonl、不留 .tmp。"""

    def test_truncate_is_atomic(self) -> None:
        cap = 3
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rows.jsonl"
            memory._append_jsonl(path, [{"i": i} for i in range(5)], cap)
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                json.loads(line)                       # 半截 jsonl 会在这里炸，正是原子写要防的
            self.assertEqual(len(lines), cap)
            self.assertEqual(list(Path(d).glob("*.tmp")), [])


class SiblingNormalizeTest(unittest.TestCase):
    """B3：同名不同 id（CS2_029 / CORE_CS2_029）按牌名归一后才算命中。"""

    def test_cards_db_matches_across_versions(self) -> None:
        db = CardDB([
            Card(id="CS2_029", name="火球术", type="SPELL"),
            Card(id="CORE_CS2_029", name="火球术", type="SPELL"),
        ])
        d1 = SavedDeck("1", "火球法A", "code", "MAGE", {"CS2_029": 2})
        d2 = SavedDeck("2", "火球法B", "code", "MAGE", {"CS2_029": 2, "OTHER": 1})
        seen = ["CORE_CS2_029"]

        _, score_with_db = decks_mod.best_match([d1, d2], seen, "MAGE", db)
        _, score_hard = decks_mod.best_match([d1, d2], seen, "MAGE")

        self.assertGreater(score_with_db, 0)
        self.assertEqual(score_hard, 0.0)


class PassiveRulePerClassTest(unittest.TestCase):
    """B4：被动规则按职业键缓存，两个职业互不覆盖。"""

    def setUp(self) -> None:
        advisor._PASSIVE_RULE.clear()

    def tearDown(self) -> None:
        advisor._PASSIVE_RULE.clear()

    def test_two_classes_coexist(self) -> None:
        db = CardDB([
            Card(id="HP_MAGE", name="火焰冲击", type="HERO_POWER", card_class="MAGE"),
            Card(id="HP_WARRIOR", name="叠甲", type="HERO_POWER", card_class="WARRIOR"),
        ])

        def make_state(class_en: str, hp_id: str):
            ours = Side(player_id=1, class_en=class_en,
                        hero_power={"id": hp_id}, deck_count=0)
            opp = Side(player_id=2, class_en="NEUTRAL", hand_count=0, deck_count=0)

            class St:
                cards = db
                turn = 1
                our_id = 1

                def sides(self):
                    return ours, opp

                def effective_turn_owner(self):
                    return 1

            return St()

        def fake_passive_rule(hp_card):
            return {"kind": hp_card.card_class.lower() + "_rule"}

        with mock.patch("hsai.advisor.policy.passive_rule",
                        side_effect=fake_passive_rule):
            b_mage = board_from_state(make_state("MAGE", "HP_MAGE"))
            b_warrior = board_from_state(make_state("WARRIOR", "HP_WARRIOR"))

        self.assertEqual(b_mage.sides[0].passive_rule, {"kind": "mage_rule"})
        self.assertEqual(b_warrior.sides[0].passive_rule, {"kind": "warrior_rule"})
        self.assertEqual(set(advisor._PASSIVE_RULE), {"MAGE", "WARRIOR"})
        self.assertTrue(all(isinstance(v, dict)
                            for v in advisor._PASSIVE_RULE.values()))



EMPTY_DB = cards.CardDB([])   # 离线空库：不触发卡牌下载、不写 data/ 缓存


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="hsai_audit_startup_"))


def _game_over_state() -> GameState:
    """结算帧：照抄 test_advice_gameover 的构造，能一路跑完 snapshot 到记录分支。"""
    st = GameState(EMPTY_DB)
    st.turn = 9
    st.turn_owner = 1
    st.phase = "game_over"
    st.game_started = True
    st.game_over = True
    st.winner = 1
    st.our_id, st.opp_id = 1, 2
    st.player_ids = {1: 10, 2: 11}
    st.entities[10] = Entity(10, tags={"PLAYER_ID": 1, "HERO_ENTITY": 20})
    st.entities[11] = Entity(11, tags={"PLAYER_ID": 2, "HERO_ENTITY": 21})
    st.entities[20] = Entity(20, card_id="HERO_10", controller=1, zone=ZONE_PLAY,
                             tags={"HEALTH": 30})
    st.entities[21] = Entity(21, card_id="HERO_10", controller=2, zone=ZONE_PLAY,
                             tags={"HEALTH": 2})
    return st


class Defect4ReplayNonUtf8(unittest.TestCase):
    def test_replay_with_invalid_utf8_does_not_raise(self) -> None:
        p = _tmp_dir() / "bad.log"
        p.write_bytes(b"first line\n\xff bad bytes\nlast line\n")
        # 修复前 read_text 不带 errors，0xff 直接抛 UnicodeDecodeError
        src = ReplaySource(p, EMPTY_DB)
        self.assertEqual(len(src.lines), 3)
        self.assertIn("last line", src.lines)


class Defect5SetupLogFailure(unittest.TestCase):
    def _run(self, results: list[tuple[Path, str]]) -> tuple[int, str]:
        args = argparse.Namespace(auto=True, dry_run=False, dir=None)
        buf = io.StringIO()
        with mock.patch.object(logwatch, "log_config_targets", return_value=[Path("p")]), \
                mock.patch.object(logwatch, "setup_log_config", return_value=results), \
                mock.patch.object(logwatch, "find_power_log", return_value=None), \
                redirect_stdout(buf):
            rc = cmd_setup_log(args)
        return rc, buf.getvalue()

    def test_write_failure_is_not_reported_as_enabled(self) -> None:
        # 修复前 changed 为空就走快速路径，谎报「已开启」并 return 0
        rc, out = self._run([(Path("p"), "写入失败：权限不足")])
        self.assertEqual(rc, 1, "写不进去不能报成功：启动脚本靠 errorlevel 提醒用户")
        self.assertNotIn("日志开关：已开启", out)
        self.assertIn("写入失败", out, "要落到逐条打印，把失败原因摆出来")

    def test_normal_create_still_returns_zero(self) -> None:
        # 收紧条件不能破坏正常创建分支的返回码
        rc, out = self._run([(Path("p"), "已创建")])
        self.assertEqual(rc, 0)
        self.assertIn("生效方式", out)


class Defect6TailStartOffset(unittest.TestCase):
    def test_no_create_game_returns_file_size(self) -> None:
        p = _tmp_dir() / "no_anchor.log"
        p.write_bytes(b"D 00:00:00.0 some line\n" * 3)
        # 修复前找不到锚点返回 0，等于从头重放好几局旧对局
        self.assertEqual(logwatch.tail_start_offset(p), p.stat().st_size)


class Defect7LogErrImport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _tmp_dir()
        patcher = mock.patch.object(logutil, "LOG_FILE", self.tmp / "hsai.log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_finish_game_failure_does_not_break_snapshot(self) -> None:
        adv = Advisor(EMPTY_DB, meta_path=self.tmp / "meta")
        adv._record_result = lambda *a, **k: None  # type: ignore[method-assign]
        st = _game_over_state()
        with mock.patch.object(adv, "_finish_game", side_effect=RuntimeError("boom")):
            # 修复前 _log_err 引用未导入的 logutil，NameError 冒出 snapshot
            adv.snapshot(st)
        self.assertTrue(adv._recorded, "记录开关必须可达，否则每帧重复入库")
        self.assertIn("finish_game 失败",
                      (self.tmp / "hsai.log").read_text(encoding="utf-8"))


class Defect8BadMetaFields(unittest.TestCase):
    def test_bad_numeric_fields_are_kept_and_do_not_raise(self) -> None:
        d = _tmp_dir() / "meta"
        d.mkdir()
        # 一个坏字段一个文件，免得前一个字段先把整份判失败、测不到后一个
        bad = {
            "prior.json": {"prior": "高"},
            "count.json": {"cards": [{"id": "X", "count": "x"}]},
            "prior_base.json": {"prior_base": []},
        }
        for name, extra in bad.items():
            payload = {"id": name, "name": "坏", "class": "WARRIOR",
                       "prior": 1.0, "cards": []}
            payload.update(extra)
            (d / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # 修复前 float/int 在 try 块外抛穿，load 与 save 都会崩
        loaded = meta.MetaDB.load(EMPTY_DB, d)
        self.assertEqual(len(loaded), 0)
        meta.MetaDB([], EMPTY_DB).save(d)
        for name in bad:
            self.assertTrue((d / name).exists(), f"{name} 不该被当成已删除原型清掉")



ROOT = Path(__file__).resolve().parent.parent


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class HasOptionsTest(unittest.TestCase):
    """_has_options 是 @staticmethod，直接调，不需要 tkinter。"""

    @staticmethod
    def _snap(mana=0, hand=None, board=None, hp=None, used=True):
        our = {"mana": mana, "hand": hand or [], "board": board or [],
               "hero_power_used": used}
        if hp is not None:
            our["hero_power"] = hp
        return {"our": our}

    def test_has_options_branches(self):
        # 手牌 / 场面 / 技能三条判定路径，逐分支锁住 True/False
        cases = (
            ("手牌费用够", dict(mana=3, hand=[{"cost": 2}], hp={"cost": 2}, used=True), True),
            ("只剩超费手牌", dict(mana=1, hand=[{"cost": 5}], hp={"cost": 2}, used=True), False),
            ("场面可攻", dict(mana=0, hand=[{"cost": 9}], board=[{"can_attack": True}],
                          hp={"cost": 2}, used=True), True),
            ("场面不可攻", dict(mana=0, hand=[{"cost": 9}], board=[{"can_attack": False}],
                            hp={"cost": 2}, used=True), False),
            ("技能未用且费用够", dict(mana=2, hand=[{"cost": 9}], hp={"cost": 2}, used=False), True),
            ("无事可做", dict(mana=0, hand=[{"cost": 9}], hp={"cost": 2}, used=True), False),
        )
        for name, kw, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(AiWorkers._has_options(self._snap(**kw)), expected)

    def test_empty_snapshot(self):
        # 读不到英雄技能就当没有可用技能，空快照不该算「还能做事」
        self.assertFalse(AiWorkers._has_options({}))

    def test_hero_power_missing_does_not_crash(self):
        # 缺 hero_power 时不抛也不能误判成 0 费可用；补上技能且够费才算有路
        base = {"mana": 2, "hand": [{"cost": 9}], "board": [],
                "hero_power_used": False}
        self.assertFalse(AiWorkers._has_options({"our": base}))
        self.assertTrue(AiWorkers._has_options(
            {"our": {**base, "hero_power": {"cost": 2}}}))


class UiFacadeSingleSourceTest(unittest.TestCase):
    def test_state_functions_are_reexported_not_redefined(self):
        self.assertIs(ui._load_state, ui_state._load_state)
        self.assertIs(ui._save_state, ui_state._save_state)

    def test_theme_constants_match_ui_theme(self):
        self.assertEqual(ui.LAYOUT_VERSION, ui_theme.LAYOUT_VERSION)
        self.assertEqual(ui.DEFAULT_WIDTH, ui_theme.DEFAULT_WIDTH)

    def test_module_level_names_still_reachable(self):
        # 子模块靠 from . import ui 回查这几个名字，删了就 NameError
        for name in ("STATE_PATH", "DATA_DIR", "LAYOUT_VERSION"):
            self.assertTrue(hasattr(ui, name), name)


class CliContractTest(unittest.TestCase):
    def test_serve_accepts_check(self):
        self.assertIn('s2.add_argument("--check"', _source("hsai/cli.py"))

    def test_max_turns_help_is_honest(self):
        self.assertIn("仅 --trace 生效", _source("hsai/cli.py"))


class CardLoadDegradeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="_test-a5-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.cache = Path(tmp) / "cards_zhCN.json"

    def _load_with(self, text):
        self.cache.write_text(text, encoding="utf-8")
        with mock.patch.object(cards, "cache_path", return_value=self.cache):
            return cards.load(auto_update=False)

    def test_bad_payloads_degrade_to_empty_db_and_good_one_reads(self):
        for label, text in (("缺 rows 键", "{}"),
                            ("顶层是 list", "[]"),
                            ("坏 JSON", "{not json")):
            with self.subTest(label=label):
                self.assertEqual(len(self._load_with(text)), 0)
        row = cards._to_row(cards.Card(id="CS2_029", name="火球术", cost=4))
        good = self._load_with(json.dumps({"rows": [row]}))
        self.assertEqual(len(good), 1)
        self.assertIsNotNone(good.get("CS2_029"))


class ReviewSummarizeTest(unittest.TestCase):
    def test_hand_overflow_reported_and_capped(self):
        turns = [{"turn": t, "hand": 10} for t in range(1, 9)]
        out = review.summarize({"turns": turns})
        line = next(x for x in out if "手牌已满" in x)
        self.assertIn("再过牌会烧牌", line)
        self.assertLessEqual(len(out), 5)

    def test_hand_below_ten_not_reported(self):
        out = review.summarize({"turns": [{"turn": 3, "hand": 9}]})
        self.assertFalse(any("手牌已满" in x for x in out), out)

    def test_lethal_and_overflow_both_kept_within_five(self):
        turns = [{"turn": t, "hand": 10, "lethal_available": True,
                  "lethal_damage": 22, "killed": False} for t in range(1, 9)]
        out = review.summarize({"turns": turns})
        self.assertTrue(any("有斩杀没斩" in x for x in out), out)
        self.assertTrue(any("手牌已满" in x for x in out), out)
        self.assertLessEqual(len(out), 5)


class TagsAiWordingTest(unittest.TestCase):
    def test_no_hardcoded_ten_classes(self):
        src = _source("hsai/tags_ai.py")
        self.assertNotIn("固定 10 类", src)
        self.assertIn("只能来自 {len(TAG_ORDER)} 类", src)


if __name__ == "__main__":
    unittest.main()
