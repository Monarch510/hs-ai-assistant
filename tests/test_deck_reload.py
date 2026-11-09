"""每局开始重读卡组、局内冻结：用户可能在两局之间改过牌。

信号跟 ui._update 同源：GameState 换对象或 _create_ts 变化才算新局。
首帧只认基线（__init__ 已读过），否则会覆盖调用方刚设的卡组字段。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hsai import advisor, cards as cards_mod, state
from hsai.advisor import Advisor

FIXTURE = Path(__file__).parent / "fixtures" / "sample_game.log"
MULLIGAN_LINES = 40      # 留牌阶段为止的日志前缀，避免局末写盘副作用


class DeckReloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = cards_mod.load()
        cls.text = "\n".join(
            FIXTURE.read_text(encoding="utf-8").splitlines()[:MULLIGAN_LINES])

    def _state(self, ts: str):
        st = state.replay_log_text(self.text, self.db)
        st._create_ts = ts
        return st

    @staticmethod
    def _write_deck(path: Path, name: str, ids: list[str]) -> None:
        path.write_text(json.dumps(
            {"name": name, "class": "法师", "tags": [], "cards": ids},
            ensure_ascii=False), encoding="utf-8")

    def _ids(self, start: int, end: int) -> list[str]:
        return [c.id for c in list(self.db.by_id.values())[start:end]]

    def test_create_ts_change_reloads_deck(self) -> None:
        ids_a = self._ids(0, 3)
        ids_b = self._ids(3, 5)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "our_deck.json"
            self._write_deck(path, "甲卡组", ids_a)
            with mock.patch.object(advisor, "OUR_DECK_PATH", path), \
                    mock.patch.object(advisor.decks_mod, "load", return_value=[]):
                adv = Advisor(self.db)
                st = self._state("game-1")
                adv.snapshot(st)                       # 首帧只认基线
                self.assertEqual(adv.our_deck, ids_a)
                self.assertEqual(adv.our_deck_name, "甲卡组")
                self.assertEqual(st.deck_size, 3)

                adv._auto_deck = mock.sentinel.auto    # 上一局自动认出的那套得作废
                self._write_deck(path, "乙卡组", ids_b)  # 两局之间改了牌
                st._create_ts = "game-2"              # 回放里同一对象靠时间戳换局
                snap = adv.snapshot(st)
                self.assertEqual(adv.our_deck, ids_b)
                self.assertEqual(adv.our_deck_name, "乙卡组")
                self.assertEqual(st.deck_size, 2, "牌库反推要跟新卡组同源")
                self.assertIsNone(adv._auto_deck)
                self.assertEqual(snap["deck"]["name"], "乙卡组")
                self.assertTrue(set(c["id"] for c in snap["deck"]["cards"]) <= set(ids_b))

    def test_new_state_object_reloads_deck(self) -> None:
        # logwatch 每局新建 GameState，时间戳可能还没变，对象身份必须也能触发重读
        ids_a = self._ids(0, 3)
        ids_b = self._ids(3, 6)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "our_deck.json"
            self._write_deck(path, "甲卡组", ids_a)
            with mock.patch.object(advisor, "OUR_DECK_PATH", path), \
                    mock.patch.object(advisor.decks_mod, "load", return_value=[]):
                adv = Advisor(self.db)
                adv.snapshot(self._state("game-1"))
                self._write_deck(path, "乙卡组", ids_b)
                adv.snapshot(self._state("game-1"))   # 同时间戳，换了对象
                self.assertEqual(adv.our_deck, ids_b)
                self.assertEqual(adv.our_deck_name, "乙卡组")

    def test_deck_frozen_within_game(self) -> None:
        # 局中不能换卡组，半局改文件不该让显示跳变
        ids_a = self._ids(0, 3)
        ids_b = self._ids(3, 5)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "our_deck.json"
            self._write_deck(path, "甲卡组", ids_a)
            with mock.patch.object(advisor, "OUR_DECK_PATH", path), \
                    mock.patch.object(advisor.decks_mod, "load", return_value=[]):
                adv = Advisor(self.db)
                st = self._state("game-1")
                adv.snapshot(st)
                self._write_deck(path, "乙卡组", ids_b)
                snap = adv.snapshot(st)               # 同对象同时间戳
                self.assertEqual(adv.our_deck, ids_a)
                self.assertEqual(adv.our_deck_name, "甲卡组")
                self.assertEqual(snap["deck"]["name"], "甲卡组")

    def test_first_game_reloads_after_pregame(self) -> None:
        # 用户拍板「含第一局」：启动后的空状态只认基线，第一局开始仍要重读
        ids_a = self._ids(0, 3)
        ids_b = self._ids(3, 5)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "our_deck.json"
            self._write_deck(path, "甲卡组", ids_a)
            with mock.patch.object(advisor, "OUR_DECK_PATH", path), \
                    mock.patch.object(advisor.decks_mod, "load", return_value=[]):
                adv = Advisor(self.db)
                pregame = self._state("")           # 启动后还没开局
                adv.snapshot(pregame)               # 首帧只认基线
                self._write_deck(path, "乙卡组", ids_b)  # 开第一局前改了牌
                pregame._create_ts = "game-1"      # 回放里原地换局
                adv.snapshot(pregame)
                self.assertEqual(adv.our_deck, ids_b)
                self.assertEqual(adv.our_deck_name, "乙卡组", "第一局也算一局，得重读")

    def test_first_frame_keeps_manually_set_fields(self) -> None:
        # 首帧无条件重读会把调用方刚清空的标签/原型从卡组文件灌回来
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "our_deck.json"
            self._write_deck(path, "甲卡组", self._ids(0, 3))
            with mock.patch.object(advisor, "OUR_DECK_PATH", path), \
                    mock.patch.object(advisor.decks_mod, "load", return_value=[]):
                adv = Advisor(self.db)
                adv.our_tags = []
                adv.our_archetype = ""
                adv.our_deck_name = "手工"
                adv.snapshot(self._state("game-1"))
                self.assertEqual(adv.our_deck_name, "手工")
                self.assertEqual(adv.our_tags, [])
                self.assertEqual(adv.our_archetype, "")


if __name__ == "__main__":
    unittest.main()
