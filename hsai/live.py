"""数据源：决定「现在看的是真实日志还是示例回放」，并支持中途切换。

从原来的 HTTP 服务里拆出来 —— 现在没有网页了，悬浮窗直接在这里拿状态。
两个必须支持的场景没变：
  1. 先开助手、后开游戏 —— 日志出现了要自动切过去。
  2. 游戏重启后日志会跑到新的会话子目录 —— 要跟着最新的那个走。
"""

from __future__ import annotations

import time
from pathlib import Path

from . import powerlog
from .cards import CardDB
from .paths import ROOT
from .logwatch import PowerLogWatcher, find_power_log
from .state import GameState

FIXTURE = ROOT / "tests" / "fixtures" / "sample_game.log"


class LogSource:
    is_demo = False

    def __init__(self, path: Path, cards: CardDB) -> None:
        self.watcher = PowerLogWatcher(path, cards)
        self.path = path
        self.name = f"日志：{path}"

    def poll(self) -> GameState | None:
        return self.watcher.state if self.watcher.poll() else None


class ReplaySource:
    is_demo = True

    def __init__(self, path: Path, cards: CardDB, *, lines_per_tick: int = 3, loop: bool = True) -> None:
        self.path = path
        self.cards = cards
        self.lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        self.lines_per_tick = lines_per_tick
        self.loop = loop
        self.idx = 0
        self.state = GameState(cards)
        self.pending: int | None = None
        self.name = f"回放：{path.name}"

    def poll(self) -> GameState | None:
        if self.idx >= len(self.lines):
            if not self.loop:
                return None
            self.idx = 0
            self.state = GameState(self.cards)
            self.pending = None
        end = min(len(self.lines), self.idx + self.lines_per_tick)
        for line in self.lines[self.idx:end]:
            ev = powerlog.parse_line(line, self.pending)
            if ev is None:
                continue
            if ev.kind == "create_game":
                self.pending = None
            elif ev.kind in powerlog.CONTEXT_KINDS and ev.data.get("entity"):
                self.pending = ev.data["entity"]
            self.state.apply(ev)
        self.idx = end
        return self.state


class SourceManager:
    CHECK_INTERVAL = 5.0

    def __init__(self, cards: CardDB, *, demo: bool = False, log_path: Path | None = None,
                 lines_per_tick: int = 3) -> None:
        self.cards = cards
        self.forced_demo = demo
        self.log_path = log_path
        self.lines_per_tick = lines_per_tick
        self.source: LogSource | ReplaySource = self._initial()
        self._last_check = 0.0

    def _initial(self) -> LogSource | ReplaySource:
        if not self.forced_demo:
            p = self.log_path or find_power_log()
            if p is not None:
                return LogSource(p, self.cards)
        path = self.log_path if (self.log_path and Path(self.log_path).exists()) else FIXTURE
        return ReplaySource(path, self.cards, lines_per_tick=self.lines_per_tick)

    @property
    def is_demo(self) -> bool:
        return bool(getattr(self.source, "is_demo", False))

    @property
    def name(self) -> str:
        return getattr(self.source, "name", "")

    def _maybe_switch(self) -> bool:
        if self.forced_demo or self.log_path:
            return False
        now = time.time()
        if now - self._last_check < self.CHECK_INTERVAL:
            return False
        self._last_check = now
        newest = find_power_log()
        if newest is None:
            return False
        if isinstance(self.source, LogSource) and self.source.watcher.path == newest:
            return False
        self.source = LogSource(newest, self.cards)
        return True

    def poll(self) -> tuple[GameState | None, bool]:
        switched = self._maybe_switch()
        return self.source.poll(), switched
