"""「奥秘 / 任务」的正确识别：SECRET 区里其实混着三类实体。

真实 Power.log 证据（本机 Hearthstone_2026_11_07_13_43_49/Power.log）：
  · 任务 / 目标牌打出后 tag=ZONE value=SECRET，带 QUEST=1 或 OBJECTIVE=1
    与 QUEST_PROGRESS_TOTAL=N，进度走带实体引用的 QUEST_PROGRESS value=N；
  · 咒符同样进 SECRET 区，带 SIGIL=1；奥秘带 SECRET=1；
  · 隐藏实体 cardId= 为空，客户端照样把它放进 SECRET 区。

另外 SHOW_ENTITY 块里的裸 tag 行没有自己的实体引用，必须由解析层补上下文：
实时路径已经这么做了，parse_text / ReplaySource 两条离线路径漏了，
于是 QUEST_PROGRESS_TOTAL 被记到上一个实体甚至 GameEntity 头上。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hsai import cards, powerlog as pl, state
from hsai.cards import Card, CardDB
from hsai.state_types import Entity, ZONE_GRAVE, ZONE_SECRET

from tests._ui_support import prepare

prepare()                       # 没有 tkinter 就装替身，别整组跳过

import hsai.ui as ui_mod        # noqa: E402  必须在 prepare() 之后
from hsai import advisor as advisor_mod  # noqa: E402
from hsai import ai as ai_mod, live, ui_text  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "secret_zone_excerpt.log"


def _db() -> CardDB:
    return CardDB([
        Card(id="BE_726", name="克苏恩仪式", type="SPELL", cost=1,
             text="2回合后，选择并触发一项克苏恩的技能。"),
        Card(id="TLC_460", name="禁忌序列", type="SPELL", cost=1,
             mechanics=("QUEST",), text="<b>任务：</b><b>发现</b>8张牌。"),
        Card(id="CATA_528", name="海洋咒符", type="SPELL", cost=1,
             text="在你的下个回合开始时，召唤一个3/3。"),
        Card(id="EX1_295", name="寒冰屏障", type="SPELL", cost=3,
             mechanics=("SECRET",), text="<b>奥秘：</b>当你的英雄将要承受致命伤害时…"),
    ])


def _state() -> state.GameState:
    st = state.GameState(_db())
    st.game_started = True
    st.mulligan_done = True
    st.phase = "playing"
    st.turn = 5
    st.our_id, st.opp_id = 1, 2
    st._id_by_hand_done = True      # 手工摆的实体别触发「按可见手牌翻面」
    return st


def _put(st: state.GameState, eid: int, card_id: str | None, *,
         ctrl: int = 1, tags: dict | None = None,
         zone: int = ZONE_SECRET) -> Entity:
    e = Entity(id=eid, card_id=card_id, controller=ctrl, zone=zone,
               tags=dict(tags or {}))
    st.entities[eid] = e
    return e


class BareTagContextTest(unittest.TestCase):
    """SHOW_ENTITY 块的裸 tag 必须归到那张牌，不是上一个实体。"""

    def test_show_entity_bare_tags_belong_to_the_card(self) -> None:
        events = pl.parse_text(FIXTURE.read_text(encoding="utf-8"))
        got: dict[str, list[tuple]] = {}
        for ev in events:
            if ev.kind == "tag_change":
                got.setdefault(ev.data["tag"], []).append(
                    (ev.data.get("entity"), ev.data.get("value")))
        self.assertIn((6, 1), got.get("SIGIL", []), "咒符的 SIGIL 要挂在 id=6")
        self.assertIn((6, 5), got.get("ZONE", []), "咒符进 SECRET 区")
        self.assertIn((43, 2), got.get("QUEST_PROGRESS_TOTAL", []))
        self.assertIn((43, 1), got.get("OBJECTIVE", []))
        self.assertIn((23, 8), got.get("QUEST_PROGRESS_TOTAL", []))
        self.assertIn((23, 1), got.get("QUEST", []))
        self.assertIn((23, 5), got.get("ZONE", []), "任务打出进 SECRET 区")

    def test_replay_source_uses_the_same_rule(self) -> None:
        src = live.ReplaySource(FIXTURE, _db(), lines_per_tick=1000, loop=False)
        st = src.poll()
        self.assertIsNotNone(st)
        self.assertEqual(st.entities[6].tags.get("SIGIL"), 1)
        self.assertEqual(st.entities[43].tags.get("OBJECTIVE"), 1)
        self.assertEqual(st.entities[43].tags.get("QUEST_PROGRESS_TOTAL"), 2)
        self.assertEqual(st.entities[23].tags.get("QUEST"), 1)
        self.assertEqual(st.entities[23].tags.get("QUEST_PROGRESS_TOTAL"), 8)
        self.assertEqual(st.entities[43].tags.get("QUEST_PROGRESS"), 1)
        self.assertEqual(st.entities[23].tags.get("QUEST_PROGRESS"), 2)

    def test_game_entity_is_not_polluted(self) -> None:
        src = live.ReplaySource(FIXTURE, _db(), lines_per_tick=1000, loop=False)
        st = src.poll()
        game = st.entities.get(1)
        if game is not None:
            self.assertIsNone(game.tags.get("QUEST_PROGRESS_TOTAL"))
            self.assertNotEqual(game.tags.get("CARDTYPE"), "SPELL")


class SecretZoneClassificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.st = _state()

    def test_three_kinds_are_split(self) -> None:
        _put(self.st, 10, None, tags={"SECRET": 1})           # 隐藏奥秘
        _put(self.st, 11, "EX1_295")                          # 卡牌库认奥秘
        _put(self.st, 12, "BE_726", tags={"OBJECTIVE": 1,
                                          "QUEST_PROGRESS_TOTAL": 2,
                                          "QUEST_PROGRESS": 1})
        _put(self.st, 13, "TLC_460", tags={"QUEST": 1,
                                           "QUEST_PROGRESS": 3,
                                           "QUEST_PROGRESS_TOTAL": 8})
        _put(self.st, 14, "CATA_528", tags={"SIGIL": 1})
        ours, opp = self.st.sides()
        self.assertEqual(ours.secrets, ["EX1_295", ""])
        self.assertEqual([q["name"] for q in ours.quests], ["克苏恩仪式", "禁忌序列"])
        self.assertEqual([q["progress"] for q in ours.quests], [1, 3])
        self.assertEqual([q["total"] for q in ours.quests], [2, 8])
        self.assertEqual([o["name"] for o in ours.ongoing], ["海洋咒符"])
        self.assertEqual(opp.quests, [])

    def test_hidden_entity_counts_as_unknown_secret(self) -> None:
        _put(self.st, 20, None)                               # 什么标签都没有
        ours, _ = self.st.sides()
        self.assertEqual(ours.secrets, [""])

    def test_card_db_recognizes_secret_and_quest(self) -> None:
        _put(self.st, 21, "EX1_295")                          # mechanics SECRET
        _put(self.st, 22, "TLC_460", tags={"QUEST_PROGRESS_TOTAL": 8})
        ours, _ = self.st.sides()
        self.assertEqual(ours.secrets, ["EX1_295"])
        self.assertEqual(ours.quests[0]["name"], "禁忌序列")

    def test_sigil_without_log_tag_is_ongoing(self) -> None:
        _put(self.st, 23, "CATA_528")                         # 只剩卡牌库可查
        ours, _ = self.st.sides()
        self.assertEqual(ours.secrets, [])
        self.assertEqual([o["name"] for o in ours.ongoing], ["海洋咒符"])

    def test_finished_quest_leaving_zone_is_not_shown(self) -> None:
        _put(self.st, 24, "TLC_460", zone=ZONE_GRAVE,
             tags={"QUEST": 1, "QUEST_PROGRESS": 8, "QUEST_PROGRESS_TOTAL": 8})
        ours, _ = self.st.sides()
        self.assertEqual(ours.quests, [], "已完成离场的任务不能再挂在面板上")

    def test_opponent_zone_is_separate(self) -> None:
        _put(self.st, 25, "BE_726", ctrl=2,
             tags={"OBJECTIVE": 1, "QUEST_PROGRESS_TOTAL": 2, "QUEST_PROGRESS": 1})
        ours, opp = self.st.sides()
        self.assertEqual(ours.quests, [])
        self.assertEqual(opp.quests[0]["name"], "克苏恩仪式")

    def test_as_dict_exposes_all_three(self) -> None:
        _put(self.st, 26, "EX1_295")
        _put(self.st, 27, "BE_726", tags={"OBJECTIVE": 1})
        _put(self.st, 28, "CATA_528", tags={"SIGIL": 1})
        d = self.st.sides()[0].as_dict()
        self.assertEqual(len(d["secrets"]), 1)
        self.assertEqual(len(d["quests"]), 1)
        self.assertEqual(len(d["ongoing"]), 1)


class HudAndReviewTest(unittest.TestCase):
    def test_hud_row_shows_three_kinds_separately(self) -> None:
        side = {"health": 27, "hand_count": 5, "deck_count": 18, "board": [],
                "secrets": ["EX1_295", ""],
                "quests": [{"id": "TLC_460", "name": "禁忌序列",
                            "progress": 3, "total": 8}],
                "ongoing": [{"id": "CATA_528", "name": "海洋咒符"}]}
        row = ui_text.hud_row(side, "对手")
        self.assertIn("奥秘 2", row)
        self.assertIn("未知 1", row)
        self.assertIn("任务 禁忌序列 3/8", row)
        self.assertIn("持续法术 1", row)

    def test_hud_row_has_one_implementation(self) -> None:
        # 门面曾重复定义 hud_row，覆盖 ui_text 的导出：界面用后者、测试用前者，
        # 改文案只改一处就会悄悄分叉。
        self.assertIs(ui_mod.hud_row, ui_text.hud_row)

    def test_snapshot_exposes_quest_for_review(self) -> None:
        st = _state()
        _put(st, 30, "TLC_460", tags={"QUEST": 1, "QUEST_PROGRESS": 3,
                                      "QUEST_PROGRESS_TOTAL": 8})
        with tempfile.TemporaryDirectory(prefix="hsai-secret-zone-") as tmp:
            adv = advisor_mod.Advisor(_db(), meta_path=Path(tmp))
            snap = adv.snapshot(st)
        self.assertEqual(snap["quest"]["name"], "禁忌序列")
        self.assertEqual(snap["quest"]["progress"], 3)
        self.assertEqual(snap["quest"]["total"], 8)
        prompt = ai_mod.build_prompt(snap)
        self.assertIn("我方任务：禁忌序列", prompt)
        self.assertIn("3/8", prompt)


if __name__ == "__main__":
    unittest.main()
