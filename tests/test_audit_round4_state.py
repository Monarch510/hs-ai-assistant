"""输入/状态层审查第 4 轮回归：N1–N6 + live 显式 --log 路径。

先写复现用例再改源码（Karpathy）。只用标准库；不读 data/，不联网。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hsai import cards, decks, live, logwatch, state
from hsai import powerlog as pl  # noqa: F401  （保持与既有测试的 import 面一致）

FIXTURE = Path(__file__).parent / "fixtures" / "real_client_excerpt.log"
P = "D 20:00:00.0000000 GameState.DebugPrintPower() - "


def _log(lines: list[str]) -> str:
    return "\n".join(P + line for line in lines) + "\n"


def _resolved(size: int = 30) -> SimpleNamespace:
    return SimpleNamespace(size=size, card_class="MAGE",
                           counts={f"X{i}": 1 for i in range(size)})


class N1SetupLogConfigTest(unittest.TestCase):
    """坏配置不能因为「别段有 LogLevel=1」就被判成已开启。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="hsai_n1_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "Hearthstone.exe").write_text("", encoding="utf-8")
        self.target = self.root / "log.config"

    def _setup(self, **kw):
        return logwatch.setup_log_config([self.root], include_ambient=False, **kw)

    def test_other_section_level1_power_level0_is_overwritten(self):
        self.target.write_text("[Zone]\nLogLevel=1\n\n[Power]\nLogLevel=0\n", encoding="utf-8")
        results = self._setup()
        self.assertEqual(self.target.read_text(encoding="utf-8"), logwatch.LOG_CONFIG_BODY)
        self.assertFalse(any("已开启" in msg for _, msg in results), results)

    def test_power_missing_flags_is_overwritten(self):
        self.target.write_text("[Power]\nLogLevel=1\n", encoding="utf-8")
        results = self._setup()
        self.assertEqual(self.target.read_text(encoding="utf-8"), logwatch.LOG_CONFIG_BODY)
        self.assertFalse(any("已开启" in msg for _, msg in results), results)

    def test_exact_body_is_enabled_without_touching_file(self):
        self.target.write_text(logwatch.LOG_CONFIG_BODY, encoding="utf-8")
        results = self._setup()
        self.assertTrue(any("已开启" in msg for _, msg in results), results)
        self.assertEqual(self.target.read_text(encoding="utf-8"), logwatch.LOG_CONFIG_BODY)
        self.assertFalse((self.root / "log.config.bak").exists())

    def test_write_failure_is_not_reported_as_enabled(self):
        self.target.write_text("[Zone]\nLogLevel=1\n\n[Power]\nLogLevel=0\n", encoding="utf-8")
        with mock.patch.object(Path, "write_text", side_effect=OSError("denied")):
            results = self._setup()
        self.assertTrue(any("写入失败" in msg for _, msg in results), results)
        self.assertFalse(any("已开启" in msg for _, msg in results), results)


class N2ResourcesZeroTest(unittest.TestCase):
    """RESOURCES=0 是合法读数，不能被「没读到」的回退覆盖。"""

    def _player(self, tags: dict) -> state.GameState:
        st = state.GameState()
        st.entities[10] = state.Entity(id=10, controller=1, tags=tags)
        st.player_ids[1] = 10
        st.turn = 3
        return st

    def test_resources_zero_keeps_zero_mana(self):
        st = self._player({state.T_RESOURCES: 0, state.T_PLAYER_ID: 1})
        s = st._side(1)
        self.assertEqual((s.mana, s.max_mana), (0, 0))

    def test_no_resources_still_falls_back_to_turn(self):
        st = self._player({state.T_PLAYER_ID: 1})
        s = st._side(1)
        self.assertEqual((s.mana, s.max_mana), (2, 2))

    def test_no_player_entity_still_falls_back_to_turn(self):
        st = state.GameState()
        st.turn = 3
        s = st._side(1)
        self.assertEqual((s.mana, s.max_mana), (2, 2))


class N3ShowPendingTest(unittest.TestCase):
    """SHOW_ENTITY 之后 GameEntity 的 ZONE=PLAY 不许被归还给真身。"""

    def test_synthetic_bare_zone_play_does_not_fabricate_a_play(self):
        text = _log([
            "CREATE_GAME",
            "GameEntity EntityID=1",
            "FULL_ENTITY - Creating ID=26 CardID=TIME_064",
            "tag=49 value=2",       # ZONE=DECK
            "tag=50 value=1",       # CONTROLLER=1
            "SHOW_ENTITY - Updating Entity=26 CardID=TIME_064",
            "tag=49 value=3",       # ZONE=HAND（SHOW 块裸 tag 归真身）
            "TAG_CHANGE Entity=1 tag=49 value=1",   # 另一条管线 GameEntity 的 ZONE=PLAY
        ])
        st = state.replay_log_text(text)
        self.assertEqual(st.entities[26].zone, state.ZONE_HAND)
        self.assertNotIn("TIME_064", st.our_played)

    def test_real_fixture_does_not_fabricate_time_064(self):
        st = state.replay_log_text(FIXTURE.read_text(encoding="utf-8"))
        self.assertNotIn("TIME_064", st.our_played)
        self.assertEqual(st.entities[12].zone, state.ZONE_HAND)
        self.assertEqual(st._side(1).deck_count, 36)
        self.assertEqual(st._side(2).deck_count, 26)


class N4ManaFromTurnTest(unittest.TestCase):
    """回退法力必须和 effective_turn_owner() 用同一套先手判定。"""

    def test_first_player_2_turn_1(self):
        st = state.GameState()
        st.first_player = 2
        st.turn = 1
        self.assertEqual(st.effective_turn_owner(), 2)
        self.assertEqual(st._mana_from_turn(2), (1, 1))
        self.assertEqual(st._mana_from_turn(1), (0, 0))

    def test_first_player_1_keeps_original_behavior(self):
        st = state.GameState()
        st.first_player = 1
        st.turn = 3
        self.assertEqual(st._mana_from_turn(1), (2, 2))
        self.assertEqual(st._mana_from_turn(2), (1, 1))


class N5DeckParseTest(unittest.TestCase):
    """跨卡组块不重置 name / deck_id 会让名字串味、id 冒领。"""

    def _parse(self, text):
        with mock.patch.object(decks.deckcode, "decode", return_value=object()), \
             mock.patch.object(decks.deckcode, "resolve", return_value=_resolved()):
            return {d.deck_id: d for d in decks.parse_text(text, cards.CardDB([]))}

    def test_missing_name_header_does_not_reuse_previous_name(self):
        out = self._parse("\n".join([
            "### 第一套",
            "# Deck ID: 111",
            "AAECAY0AAAAAAAAAAAAAAA=",
            "# Deck ID: 222",
            "AAECAY0AAAAAAAAAAAAAAA=",
        ]))
        self.assertEqual(out["111"].name, "第一套")
        self.assertEqual(out["222"].name, "222")

    def test_bad_code_does_not_let_later_code_claim_stale_id(self):
        bad = "AAECAAAAAAAAAAAAAAAA="

        def decode(code):
            if code == bad:
                raise decks.deckcode.DeckCodeError("bad")
            return object()

        with mock.patch.object(decks.deckcode, "decode", side_effect=decode), \
             mock.patch.object(decks.deckcode, "resolve", return_value=_resolved()):
            out = {d.deck_id: d for d in decks.parse_text("\n".join([
                "### 第一套",
                "# Deck ID: 111",
                bad,
                "AAECAY0AAAAAAAAAAAAAAA=",
            ]), cards.CardDB([]))}
        self.assertNotIn("111", out)


N6_LINES = [
    "CREATE_GAME",
    "GameEntity EntityID=1",
    "TAG_CHANGE Entity=1 tag=20 value=3",   # TURN=3
    "CREATE_GAME",                           # 同时间戳的重复 CREATE_GAME
]


class N6CreateGameDedupTest(unittest.TestCase):
    """调用方不能无条件 reset，否则 apply() 的同时间戳去重永远走不到。"""

    def test_direct_apply_dedups(self):
        self.assertEqual(state.replay_log_text(_log(N6_LINES)).turn, 3)

    def test_replay_source_dedups(self):
        with tempfile.TemporaryDirectory(prefix="hsai_n6_") as d:
            p = Path(d) / "Power.log"
            p.write_text(_log(N6_LINES), encoding="utf-8")
            src = live.ReplaySource(p, cards.CardDB([]), lines_per_tick=1000, loop=False)
            self.assertEqual(src.poll().turn, 3)

    def test_power_log_watcher_dedups(self):
        with tempfile.TemporaryDirectory(prefix="hsai_n6_") as d:
            p = Path(d) / "Power.log"
            # 前置一行让 tail_start_offset 能定位到 CREATE_GAME 的行首
            head = P + "TAG_CHANGE Entity=1 tag=937 value=1\n"
            p.write_text(head + _log(N6_LINES[:3]), encoding="utf-8")
            w = logwatch.PowerLogWatcher(p, cards.CardDB([]))
            w.poll()
            with p.open("a", encoding="utf-8") as fh:
                fh.write(P + N6_LINES[3] + "\n")
            w.poll()
            self.assertEqual(w.state.turn, 3)

    def test_real_fixture_via_replay_source_keeps_first_pipeline(self):
        src = live.ReplaySource(FIXTURE, cards.CardDB([]), lines_per_tick=100000, loop=False)
        st = src.poll()
        self.assertNotEqual(st.playstate, 0)      # 没被重复的 CREATE_GAME 清成 0
        self.assertEqual(st._side(1).deck_count, 36)
        self.assertEqual(st._side(2).deck_count, 26)
        self.assertEqual(st.entities[12].zone, state.ZONE_HAND)


class LiveExplicitLogPathTest(unittest.TestCase):
    """用户 --log 指定的路径不能被自动切换到更新的真实日志。"""

    def test_explicit_log_path_is_never_switched(self):
        with tempfile.TemporaryDirectory(prefix="hsai_live_") as d:
            root = Path(d)
            user_path = root / "MyPower.log"
            user_path.write_text("", encoding="utf-8")
            newest = root / "Real" / "Power.log"
            newest.parent.mkdir()
            newest.write_text("", encoding="utf-8")
            mgr = live.SourceManager(cards.CardDB([]), log_path=user_path)
            mgr._last_check = 0.0
            with mock.patch.object(live, "find_power_log", return_value=newest):
                self.assertFalse(mgr._maybe_switch())
            self.assertIsInstance(mgr.source, live.LogSource)
            self.assertEqual(mgr.source.path, user_path)

    def test_auto_switch_still_works_without_explicit_path(self):
        with tempfile.TemporaryDirectory(prefix="hsai_live_") as d:
            root = Path(d)
            older = root / "Old" / "Power.log"
            older.parent.mkdir()
            older.write_text("", encoding="utf-8")
            newest = root / "New" / "Power.log"
            newest.parent.mkdir()
            newest.write_text("", encoding="utf-8")
            with mock.patch.object(live, "find_power_log", side_effect=[older, newest]):
                mgr = live.SourceManager(cards.CardDB([]))
                mgr._last_check = 0.0
                self.assertTrue(mgr._maybe_switch())
            self.assertIsInstance(mgr.source, live.LogSource)
            self.assertEqual(mgr.source.path, newest)


if __name__ == "__main__":
    unittest.main()
