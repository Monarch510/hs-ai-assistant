"""预判 / 留牌 / 建议 / 快照组装。"""

from __future__ import annotations
import shutil
import tempfile
from unittest import mock
from hsai import cards, review
from hsai import advisor as advisor_mod
from hsai.state import ZONE_PLAY, Entity, GameState
from hsai import cards, infer, meta

import unittest
from pathlib import Path

from hsai import cards as cards_mod
from hsai import mulligan, policy
from hsai.advisor import Advisor, board_from_state
from hsai.infer import Candidate, DeckPredictor, Prediction
from hsai.meta import Archetype, MetaDB
from hsai.model import Board, SideState, Unit, evaluate
from hsai.state import replay_log_text

FIXTURE = Path(__file__).parent / "fixtures" / "sample_game.log"


class InferTest(unittest.TestCase):
    """预判算法本身。

    这里用**自建的**两个战士原型，不读 `data/meta/`：内置种子删掉之后真实库里
    有什么完全取决于用户导入了哪些卡组，拿它当断言对象，测试就会随用户数据飘。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.meta = MetaDB([
            Archetype(id="tempo_warrior", name="节奏战", card_class="WARRIOR", prior=0.5,
                      cards={"NEW1_025": 2, "CORE_EX1_604": 2}, source="import"),
            Archetype(id="control_warrior", name="控制战", card_class="WARRIOR", prior=0.5,
                      cards={"CORE_EX1_407": 2, "CORE_CS2_108": 2}, source="import"),
        ], cls.db)
        cls.pred = DeckPredictor(cls.meta, cls.db)

    def test_posteriors_sum_to_one(self) -> None:
        post = self.pred.posteriors("WARRIOR", [("NEW1_025", 2)])
        self.assertAlmostEqual(sum(p for _, p in post), 1.0, places=6)

    def test_signature_card_shifts_prediction(self) -> None:
        tempo = self.pred.predict("WARRIOR", [("NEW1_025", 2), ("CORE_EX1_604", 3)], turn=3)
        control = self.pred.predict("WARRIOR", [("CORE_EX1_407", 5), ("CORE_CS2_108", 4)], turn=5)
        self.assertEqual(tempo.name, "节奏战")
        self.assertEqual(control.name, "控制战")
        self.assertLessEqual(tempo.confidence, 0.95, "置信度不应该顶到 100%")

    def test_evidence_is_explained(self) -> None:
        pred = self.pred.predict("WARRIOR", [("NEW1_025", 2)], turn=2)
        self.assertTrue(any("血帆海盗" in e for e in pred.evidence))

    def test_no_archetype_for_class(self) -> None:
        pred = self.pred.predict("UNKNOWN_CLASS", [], turn=1)
        self.assertEqual(pred.name, "未知卡组")
        self.assertTrue(pred.note)

    def test_prior_only_flag(self) -> None:
        pred = self.pred.predict("HUNTER", [], turn=1, hand_count=4, deck_count=26)
        self.assertTrue(pred.prior_only)


class MulliganTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()

    def _pred(self, tags):
        return Prediction(name="快攻战", candidates=[Candidate("x", "快攻战", 0.9, "战士", tags)])

    def test_aggro_keeps_cheap_and_drops_expensive(self) -> None:
        hand = [self.db.by_name_or_id("法力浮龙"), self.db.by_name_or_id("烈焰风暴"),
                self.db.by_name_or_id("水元素")]
        rows = {r["name"]: r for r in mulligan.advise(hand, cards=self.db, prediction=self._pred(["快攻"]))}
        self.assertEqual(rows["法力浮龙"]["action"], "keep")
        self.assertEqual(rows["烈焰风暴"]["action"], "toss")

    def test_reasons_present(self) -> None:
        hand = [self.db.by_name_or_id("寒冰箭")]
        rows = mulligan.advise(hand, cards=self.db, prediction=self._pred(["快攻"]))
        self.assertTrue(rows[0]["reason"])
        self.assertIn(rows[0]["action"], ("keep", "toss"))


class PolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.advisor = Advisor(cls.db)

    def _board(self, my_hp=30, opp_hp=30, my_units=(), opp_units=(), mana=10):
        b = Board([SideState(hero_hp=my_hp, mana=mana, max_mana=mana),
                   SideState(hero_hp=opp_hp, mana=0, max_mana=0)], turn=9, active=0, cards=self.db)
        for atk, hp in my_units:
            b.sides[0].board.append(Unit("", "我方随从", atk, hp, attacks_left=1))
        for atk, hp in opp_units:
            b.sides[1].board.append(Unit("", "对方随从", atk, hp, attacks_left=1))
        return b

    def test_lethal_detected(self) -> None:
        b = self._board(opp_hp=9, my_units=[(4, 4), (4, 4), (2, 2)])
        res = policy.max_face_damage(b, 0, self.db)
        self.assertTrue(res["lethal"])
        self.assertEqual(res["damage"], 10)

    def test_no_lethal_when_taunt_blocks(self) -> None:
        b = self._board(opp_hp=8, my_units=[(4, 4), (4, 4)], opp_units=[(0, 6)])
        b.sides[1].board[0].taunt = True
        res = policy.max_face_damage(b, 0, self.db)
        self.assertFalse(res["lethal"])

    def test_trades_before_face(self) -> None:
        b = self._board(my_units=[(2, 2)], opp_units=[(5, 5)])
        acts = policy.legal_actions(b, 0, self.db)
        best = policy.best_action(b, 0, self.db)
        self.assertIsNotNone(best)
        self.assertTrue(any(a.kind == "attack" for a in acts))

    def test_plan_returns_actions(self) -> None:
        st = replay_log_text(FIXTURE.read_text(encoding="utf-8"), self.db)
        plan = policy.plan_turn(board_from_state(st), self.db, side=0)
        self.assertTrue(plan["plan"], "应该给得出至少一步")
        self.assertIn("lethal", plan)

    def test_evaluate_is_symmetric(self) -> None:
        b = self._board(my_units=[(3, 3)], opp_units=[])
        self.assertGreater(evaluate(b, perspective=0), evaluate(b, perspective=1))


class SnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.advisor = Advisor(cls.db)

    def test_snapshot_contract(self) -> None:
        st = replay_log_text(FIXTURE.read_text(encoding="utf-8"), self.db)
        snap = self.advisor.snapshot(st)
        for key in ("type", "phase", "turn", "is_our_turn", "our", "opp",
                    "prediction", "advice", "result"):
            self.assertIn(key, snap)
        self.assertEqual(snap["type"], "snapshot")
        self.assertIn("candidates", snap["prediction"])
        self.assertIn("lines", snap["advice"])
        # 胜负字段：对局没结束时是空串，结束时是 win/loss
        self.assertIn(snap["result"], ("", "win", "loss"))

    def test_mulligan_section_during_mulligan(self) -> None:
        text = "\n".join((FIXTURE.read_text(encoding="utf-8").splitlines())[:40])
        st = replay_log_text(text, self.db)
        snap = self.advisor.snapshot(st)
        self.assertEqual(snap["phase"], "mulligan")
        self.assertIn("mulligan", snap)
        self.assertTrue(snap["mulligan"]["advice"])


# 并入自 tests/test_risk.py：风险概率口径：跟着对手费用变化
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

# 并入自 tests/test_advice_gameover.py：结算帧不能把上一帧的建议重算掉
DB = cards.load()


def _state() -> GameState:
    st = GameState(DB)
    st.turn = 9
    st.turn_owner = 1
    st.phase = "playing"
    st.game_started = True
    st.our_id, st.opp_id = 1, 2
    st.player_ids = {1: 10, 2: 11}
    st.entities[10] = Entity(10, tags={"PLAYER_ID": 1, "HERO_ENTITY": 20})
    st.entities[11] = Entity(11, tags={"PLAYER_ID": 2, "HERO_ENTITY": 21})
    st.entities[20] = Entity(20, card_id="HERO_10", controller=1, zone=ZONE_PLAY,
                             tags={"HEALTH": 30})
    st.entities[21] = Entity(21, card_id="HERO_10", controller=2, zone=ZONE_PLAY,
                             tags={"HEALTH": 2})
    # 场上一只 3 攻随从，对面 2 血：这回合能斩杀
    st.entities[30] = Entity(30, card_id="CS2_120", controller=1, zone=ZONE_PLAY,
                             tags={"ATK": 3, "HEALTH": 3, "ATTACKABLE": 1})
    return st


class AdviceOnGameOverTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="_test-advice-")
        self.addCleanup(shutil.rmtree, tmp, True)
        patcher = mock.patch.object(advisor_mod, "LAST_REVIEW_JSON",
                                    Path(tmp) / "last_review.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        rec_patcher = mock.patch.object(review, "append_game", lambda *a, **k: None)
        rec_patcher.start()
        self.addCleanup(rec_patcher.stop)
        self.adv = Advisor(DB)
        self.adv._record_result = lambda *a, **k: None  # type: ignore[method-assign]

    def test_game_over_keeps_last_advice(self) -> None:
        st = _state()
        playing = self.adv.snapshot(st)
        self.assertTrue((playing.get("advice") or {}).get("lethal"), playing.get("advice"))

        # 伤害先结算、PLAYSTATE 还没到的那一帧：对面已经 0 血，但 phase 还是 playing
        st.entities[21].tags["DAMAGE"] = 2
        mid = self.adv.snapshot(st)
        self.assertTrue((mid.get("advice") or {}).get("lethal"),
                        "对面已死的那一帧把斩杀建议重算掉了")

        # 击杀结算：phase 变成 game_over
        st.phase = "game_over"
        st.game_over = True
        st.winner = 1
        over = self.adv.snapshot(st)

        self.assertTrue((over.get("advice") or {}).get("lethal"),
                        "结算帧把斩杀建议重算掉了")
        self.assertEqual(
            [x.get("title") for x in over["advice"].get("lines") or []],
            [x.get("title") for x in playing["advice"].get("lines") or []],
        )
        self.assertFalse(any("有斩杀没斩" in x for x in over.get("review") or []),
                         over.get("review"))

if __name__ == "__main__":
    unittest.main()
