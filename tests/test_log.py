"""日志解析 + 状态归约。"""

from __future__ import annotations

import unittest
from pathlib import Path

from hsai import cards as cards_mod
from hsai import powerlog
from hsai.state import ZONE_HAND, ZONE_PLAY, GameState, replay_log_text

FIXTURE = Path(__file__).parent / "fixtures" / "sample_game.log"


class PowerLogParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events = powerlog.parse_text(FIXTURE.read_text(encoding="utf-8"))

    def test_sources_and_counts(self) -> None:
        kinds = [e.kind for e in self.events]
        self.assertIn("create_game", kinds)
        self.assertGreater(kinds.count("full_entity"), 5)
        self.assertGreater(kinds.count("tag_change"), 20)

    def test_entity_ref(self) -> None:
        line = ('D 20:00:14.0000000 GameState.DebugPrintPower() - TAG_CHANGE '
                'Entity=[entityName=法力浮龙 id=10 zone=HAND zonePos=3 cardId=NEW1_012 player=1] '
                'tag=49 value=1')
        ev = powerlog.parse_line(line)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.kind, "tag_change")
        self.assertEqual(ev.data["entity"], 10)
        self.assertEqual(ev.data["tag"], "ZONE")
        self.assertEqual(ev.data["value"], 1)
        self.assertEqual(ev.data["ref"]["card_id"], "NEW1_012")

    def test_symbolic_tag_values(self) -> None:
        line = 'D 20:00:14.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=1 tag=PLAYSTATE value=PLAYING'
        ev = powerlog.parse_line(line)
        self.assertEqual(ev.data["tag"], "PLAYSTATE")
        self.assertEqual(ev.data["value"], 2)

    def test_unknown_lines_are_not_errors(self) -> None:
        ev = powerlog.parse_line("D 01:02:03.4000000 GameState.DebugPrintPower() - SOMETHING_NEW x=1")
        self.assertIsNotNone(ev)
        self.assertEqual(ev.kind, "unknown")
        self.assertIsNone(powerlog.parse_line("D 01:02:03.4000000 SomethingElse() - CREATE_GAME"))
        self.assertIsNone(powerlog.parse_line(""))
        self.assertIsNone(powerlog.parse_line("garbage"))


class StateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.st = replay_log_text(FIXTURE.read_text(encoding="utf-8"), cls.db)

    def test_players_detected(self) -> None:
        self.assertEqual(self.st.our_id, 1)
        self.assertEqual(self.st.opp_id, 2)

    def test_our_id_uses_visible_hand_not_account(self) -> None:
        """练习模式两边都有真账号：只按 GameAccountId 判断会判反。

        造一个「账号指向对手」的日志：玩家 1 账号被抹掉、玩家 2 保留真账号。
        谁能看到自己的手牌，谁才是我们。
        """
        text = FIXTURE.read_text(encoding="utf-8")
        # fixture 改了格式就让这条用例先报错，别悄悄变成空转
        self.assertIn("Player EntityID=2 PlayerID=1 GameAccountId=[hi=144115193835963207 lo=1]", text)
        text = text.replace(
            "Player EntityID=2 PlayerID=1 GameAccountId=[hi=144115193835963207 lo=1]",
            "Player EntityID=2 PlayerID=1 GameAccountId=[hi=0 lo=0]",
        )
        st = replay_log_text(text, self.db)
        self.assertEqual(st.our_id, 1)
        self.assertEqual(st.opp_id, 2)

    def test_classes(self) -> None:
        ours, opp = self.st.sides()
        self.assertEqual(ours.class_cn, "法师")
        self.assertEqual(opp.class_cn, "战士")

    def test_turn_and_phase(self) -> None:
        self.assertEqual(self.st.turn, 5)
        self.assertEqual(self.st.phase, "playing")
        self.assertTrue(self.st.mulligan_done)

    def test_hand_view(self) -> None:
        ours, _ = self.st.sides()
        names = [h["name"] for h in ours.hand]
        self.assertIn("寒冰箭", names)
        self.assertIn("火球术", names)
        self.assertNotIn("法力浮龙", names, "已经打出去的牌不应该还在手牌里")

    def test_board_view(self) -> None:
        ours, opp = self.st.sides()
        self.assertEqual([u["name"] for u in ours.board], ["法力浮龙"])
        self.assertEqual([u["name"] for u in opp.board], ["暴乱狂战士"])

    def test_revealed_tracking(self) -> None:
        self.assertIn("NEW1_025", self.st.revealed_opp_order)
        self.assertIn("CORE_EX1_604", self.st.revealed_opp_order)
        self.assertEqual(self.st.revealed_turns["NEW1_025"], 2)

    def test_mana(self) -> None:
        ours, opp = self.st.sides()
        self.assertEqual((ours.mana, ours.max_mana), (3, 3))
        self.assertEqual(opp.max_mana, 2)

    def test_snapshot_shape(self) -> None:
        snap = self.st.snapshot()
        for key in ("phase", "turn", "is_our_turn", "our", "opp", "revealed_opp"):
            self.assertIn(key, snap)
        self.assertTrue(snap["is_our_turn"])


class GameOverTest(unittest.TestCase):
    def test_playstate_win(self) -> None:
        text = "\n".join([
            "D 01:00:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
            "D 01:00:00.1000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=1 tag=17 value=4",
        ])
        st = GameState(None)
        st.apply_many(powerlog.parse_text(text))
        self.assertTrue(st.game_over)
        self.assertEqual(st.phase, "game_over")


# 并入自 tests/test_refs.py：实体引用的括号配平回归
class BracketRefTest(unittest.TestCase):
    def test_three_options_with_nested_name(self) -> None:
        body = (
            "  Entities[0]=[entityName=小巧玩具 id=168 zone=SETASIDE zonePos=0 cardId=SW_034 player=1]"
            "  Entities[1]=[entityName=星空光环 id=169 zone=SETASIDE zonePos=0 cardId=GDB_140 player=1]"
            "  Entities[2]=[entityName=UNKNOWN ENTITY [cardType=INVALID] id=170 zone=SETASIDE"
            " zonePos=0 cardId=REV_240 player=1]"
        )
        refs = powerlog._all_refs(body)
        self.assertEqual(len(refs), 3)
        self.assertEqual([r["card_id"] for r in refs], ["SW_034", "GDB_140", "REV_240"])
        self.assertEqual(refs[0]["name"], "小巧玩具")
        self.assertEqual(refs[2]["zone"], powerlog.ZONE_IDS["SETASIDE"])

    def test_name_does_not_swallow_fields(self) -> None:
        body = "[entityName=幸运币 id=72 zone=HAND zonePos=5 cardId=TTN_COIN2 player=1]"
        ref = powerlog._all_refs(body)[0]
        self.assertEqual(ref["name"], "幸运币")
        self.assertEqual(ref["id"], 72)
        self.assertEqual(ref["controller"], 1)

    def test_plain_entity_is_ignored(self) -> None:
        self.assertEqual(powerlog._all_refs("Entities[0]=[id=3 zone=PLAY]")[0]["id"], 3)
        self.assertEqual(powerlog._all_refs("没有方括号的一行"), [])

if __name__ == "__main__":
    unittest.main()
