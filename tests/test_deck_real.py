"""牌库数的真实口径：初始牌库实体计数，起手 3/4 张必须计入。

回归基准是真实客户端日志片段（tests/fixtures/real_client_excerpt.log）：
覆盖 CREATE_GAME → 留牌 → 第 1 回合，GameState / PowerTaskList 双来源齐全。
玩家 1 用的是 40 张卡组（实体 4-43），玩家 2 是 30 张，正好把两种卡组都测到。
不读 data/，不联网。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hsai import cards, state
from hsai import powerlog as pl

FIXTURE = Path(__file__).parent / "fixtures" / "real_client_excerpt.log"

P = "D 20:00:00.0000000 GameState.DebugPrintPower() - "


def _log(body_lines: list[str]) -> str:
    return "\n".join(P + line for line in body_lines)


def _deck_entity(eid: int, pid: int) -> list[str]:
    """CREATE_GAME 里「凭空出现在 DECK」的实体（裸 tag 风格）。"""
    return [f"FULL_ENTITY - Creating ID={eid} CardID=",
            "tag=49 value=2",       # ZONE=DECK
            f"tag=50 value={pid}"]  # CONTROLLER


def _synth(n: int, deal: dict[int, int], tail: list[str] | None = None) -> tuple[str, dict[int, list[int]]]:
    """n 张/人的牌库 + 起手各抽 deal 张。返回 (日志文本, 双方的实体 id 列表)。"""
    lines = ["CREATE_GAME", "GameEntity EntityID=1"]
    ids: dict[int, list[int]] = {1: [], 2: []}
    eid = 4
    for pid in (1, 2):
        for _ in range(n):
            lines += _deck_entity(eid, pid)
            ids[pid].append(eid)
            eid += 1
    for pid, k in deal.items():
        for i in range(k):
            lines.append(f"TAG_CHANGE Entity={ids[pid][i]} tag=49 value=3")  # ZONE=HAND
    lines += (tail or [])
    return _log(lines), ids


class DeckRealLogTest(unittest.TestCase):
    """真实日志回归：起手扣除、上界、单调性。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = FIXTURE.read_text(encoding="utf-8")

    def test_opening_deck_subtracted(self):
        """第 1 回合：先手库 = 卡组 − 起手，起手抽牌必须计入。

        玩家 1：40 张卡组、先手起手 3 张 → 37；
        玩家 2：30 张卡组、后手起手 4 张 → 26（幸运币不是从牌库抽的，不扣）。
        修 bug 前：双方都按「30 − 抽牌数」估，开局显示 30/30。
        """
        st = state.replay_log_text(self.text)
        self.assertTrue(st.mulligan_done)
        self.assertEqual(st._side(1).deck_count, 37)
        self.assertEqual(st._side(2).deck_count, 26)

    def test_bounds_and_monotonic_after_mulligan(self):
        """全程 0 ≤ 牌库数 ≤ 卡组张数；换牌彻底结束之后只减不增。"""
        events = pl.parse_text(self.text)
        # 换牌段本身有「牌回库」(ZONE→DECK)，允许波动；最后一条
        # MULLIGAN_STATE=DONE（含 P 流副本）之后才要求严格不增。
        last_done = max(i for i, ev in enumerate(events)
                        if ev.kind == "tag_change"
                        and ev.data.get("tag") == "MULLIGAN_STATE"
                        and ev.data.get("value") == "DONE")
        st = state.GameState()
        prev = {1: None, 2: None}
        for i, ev in enumerate(events):
            st.apply(ev)
            for pid, cap in ((1, 40), (2, 30)):
                c = st._side(pid).deck_count
                self.assertGreaterEqual(c, 0, f"event#{i} player{pid}")
                self.assertLessEqual(c, cap, f"event#{i} player{pid}")
                if i > last_done:
                    self.assertLessEqual(c, prev[pid] if prev[pid] is not None else c,
                                         f"event#{i} player{pid} 牌库变多了")
                    prev[pid] = c
        self.assertEqual((prev[1], prev[2]), (37, 26))

    def test_advisor_snapshot_keeps_real_count(self):
        """advisor 快照不许再用「卡组张数 − 已抽到」把真实读数覆盖回去。"""
        from hsai import advisor as advisor_mod

        tmp = tempfile.TemporaryDirectory(prefix="hsai-deck-real-")
        self.addCleanup(tmp.cleanup)
        adv = advisor_mod.Advisor(cards.CardDB([]), meta_path=Path(tmp.name))
        st = state.replay_log_text(self.text)
        snap = adv.snapshot(st)
        self.assertEqual(snap["our"]["deck_count"], st._side(st.our_id).deck_count)


class DeckSyntheticTest(unittest.TestCase):
    """构造日志补真实片段覆盖不到的分支：封口、洗入、疲劳、两处 zone 回退。"""

    def test_show_entity_does_not_revert_zone(self):
        """SHOW_ENTITY 头部引用带的是旧快照，不能把已更新的 zone 盖回 DECK。"""
        text, _ = _synth(3, {}, tail=[
            "TAG_CHANGE Entity=4 tag=49 value=3",
            "SHOW_ENTITY - Updating Entity=[entityName=x id=4 zone=DECK zonePos=0 "
            "cardId= player=1] CardID=ABC",
        ])
        st = state.replay_log_text(text)
        self.assertEqual(st.entities[4].zone, state.ZONE_HAND)
        self.assertEqual(st._side(1).deck_count, 2)

    def test_show_entity_bare_tags_reach_real_entity(self):
        """SHOW_ENTITY 块的裸 tag 挂到 GameEntity 头上时，ZONE 要归还给真身。

        真实日志里起手/替换牌全靠这些裸 tag 进手牌（解析层把它们挂在
        最近一个带 id 的实体上，实测经常是 GameEntity）。
        """
        text, _ = _synth(3, {}, tail=[
            "BLOCK_START BlockType=TRIGGER Entity=GameEntity EffectCardId= "
            "EffectIndex=0 Target=0 SubOption=-1",
            "SHOW_ENTITY - Updating Entity=4 CardID=ABC",
            "tag=49 value=3",
            "BLOCK_END",
        ])
        st = state.replay_log_text(text)
        self.assertEqual(st.entities[4].zone, state.ZONE_HAND)
        self.assertEqual(st._side(1).deck_count, 2)

    def test_generated_deck_card_not_counted(self):
        """封口后「凭空出现在 DECK」的生成牌不算进牌库（修 bug 前实测虚到 31/34）。"""
        turn1 = ["TAG_CHANGE Entity=1 tag=20 value=1"]  # TURN=1，封口
        text, ids = _synth(30, {1: 3, 2: 4}, tail=turn1 + [
            "FULL_ENTITY - Creating ID=90 CardID=",
            "tag=49 value=2",
            "tag=50 value=1",
        ])
        st = state.replay_log_text(text)
        self.assertEqual(st._side(1).deck_count, 27)
        self.assertNotIn(90, st._deck_ids[1])
        self.assertNotIn(90, ids[1])

    def test_real_shuffle_into_deck_counts(self):
        """封口后从真实区域（备用区）TAG_CHANGE 进 DECK 的要算进去：真·洗入牌库。"""
        turn1 = ["TAG_CHANGE Entity=1 tag=20 value=1"]
        text, _ = _synth(30, {1: 3, 2: 4}, tail=turn1 + [
            "FULL_ENTITY - Creating ID=90 CardID=",
            "tag=49 value=7",       # ZONE=SETASIDE，生成时不在牌库
            "tag=50 value=1",
            "TAG_CHANGE Entity=90 tag=49 value=2",  # 洗入牌库
        ])
        st = state.replay_log_text(text)
        self.assertEqual(st._side(1).deck_count, 28)

    def test_fatigue_zero_never_negative(self):
        """牌库抽空后是 0，且继续有实体换区也不会变负。"""
        text, ids = _synth(3, {1: 3, 2: 3})
        # 手牌里那张再进场面：牌库已经 0，不许算出负数
        text += "\n" + _log([f"TAG_CHANGE Entity={ids[1][0]} tag=49 value=1"])
        st = state.replay_log_text(text)
        self.assertEqual(st._side(1).deck_count, 0)
        self.assertEqual(st._side(2).deck_count, 0)

    # 洗入牌库：我方 / 对面两侧都要 +1（用户要求，2026-11-07）
    def _shuffled(self, pid: int, eid: int, spawn_in_deck: bool) -> str:
        """生成一张带 card_id 的牌并洗入牌库。

        spawn_in_deck=False：先生成在 SETASIDE，再 TAG_CHANGE 进 DECK（洗入效果常见形态）；
        spawn_in_deck=True ：生成时直接 zone=DECK（另一类洗入效果，没有 TAG_CHANGE）。
        """
        tail = ["TAG_CHANGE Entity=1 tag=20 value=1"]  # TURN=1，封口
        if spawn_in_deck:
            tail += [
                f"FULL_ENTITY - Creating ID={eid} CardID=SHUFFLED",
                "tag=49 value=2",       # 自带 ZONE=DECK
                f"tag=50 value={pid}",
            ]
        else:
            tail += [
                f"FULL_ENTITY - Creating ID={eid} CardID=SHUFFLED",
                "tag=49 value=7",       # ZONE=SETASIDE
                f"tag=50 value={pid}",
                f"TAG_CHANGE Entity={eid} tag=49 value=2",  # 洗入 DECK
            ]
        text, _ = _synth(30, {1: 3, 2: 4}, tail=tail)
        return text

    def test_shuffle_tag_change_counts_for_both_sides(self):
        """TAG_CHANGE 洗入：我方 27→28、对面 26→27。"""
        st1 = state.replay_log_text(self._shuffled(1, 901, False))
        st2 = state.replay_log_text(self._shuffled(2, 902, False))
        self.assertEqual(st1._side(1).deck_count, 28)
        self.assertEqual(st1._side(2).deck_count, 26)
        self.assertEqual(st2._side(1).deck_count, 27)
        self.assertEqual(st2._side(2).deck_count, 27)

    def test_spawn_in_deck_counts_for_both_sides(self):
        """生成时直接 zone=DECK 的洗入：有 card_id，我方/对面都要 +1。"""
        st1 = state.replay_log_text(self._shuffled(1, 903, True))
        st2 = state.replay_log_text(self._shuffled(2, 904, True))
        self.assertEqual(st1._side(1).deck_count, 28)
        self.assertEqual(st1._side(2).deck_count, 26)
        self.assertEqual(st2._side(1).deck_count, 27)
        self.assertEqual(st2._side(2).deck_count, 27)


if __name__ == "__main__":
    unittest.main()
