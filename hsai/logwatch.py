"""跟随 Power.log：增量读取、断点续读、换局自动重置，以及「日志到底在哪」。

关于日志位置（这是本工具最容易踩的坑）：
炉石并不是只有一个标准安装位置。实测中至少见过三种：

  1. Battle.net 安装：<LOCALAPPDATA>\\Blizzard\\Hearthstone\\Logs\\Power.log
  2. 客户端安装目录旁：  D:\\software\\Hearthstone\\Logs\\Power.log
  3. 上面第 2 种还会按会话分子目录：Logs\\Hearthstone_2026_10_31_12_09_15\\Power.log

而且 log.config 只对**它所在目录对应的那个客户端**生效。所以这里不猜死路径：
先问运行中的 Hearthstone.exe 在哪，再看缓存，最后才用默认位置。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Iterable

from . import powerlog
from .cards import CardDB
from .paths import DATA_DIR, INSTALL_CACHE_TXT
from .state import GameState

# 默认位置（Battle.net 版）
READ_LIMIT = 4 * 1024 * 1024  # 首次最多回溯 4MB
INSTALL_CACHE = INSTALL_CACHE_TXT

LOG_CONFIG_BODY = """[Arena]
LogLevel=1
FilePrinting=True
ConsolePrinting=False
ScreenPrinting=False
Verbose=False

[Decks]
LogLevel=1
FilePrinting=True
ConsolePrinting=False
ScreenPrinting=False
Verbose=False

[Power]
LogLevel=1
FilePrinting=True
ConsolePrinting=False
ScreenPrinting=False
Verbose=True
"""


def _powershell(command: str, timeout: float = 6.0) -> str:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def running_hearthstone_dir() -> Path | None:
    """正在运行的 Hearthstone.exe 所在目录。这是最可靠的线索。

    每次启动 PowerShell 要几百毫秒，所以进程内缓存 60 秒 —— 这个值不需要很新，
    用户装了第二个客户端的情况极少，重启工具会重新探测。
    """
    global _proc_cache
    now = time.time()
    if _proc_cache is not None and now - _proc_cache[0] < 60.0:
        return _proc_cache[1]
    out = _powershell("(Get-Process Hearthstone -ErrorAction SilentlyContinue "
                      "| Select-Object -First 1).Path")
    path: Path | None = None
    if out:
        p = Path(out.strip().strip('"'))
        path = p.parent if p.exists() else None
    _proc_cache = (now, path)
    return path


_proc_cache: tuple[float, Path | None] | None = None


def cached_install_dir() -> Path | None:
    try:
        text = INSTALL_CACHE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    p = Path(text)
    return p if p.exists() else None


def remember_install_dir(path: Path) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        INSTALL_CACHE.write_text(str(path), encoding="utf-8")
    except OSError:
        pass


def candidate_roots(extra: Iterable[Path] = (), *, include_ambient: bool = True) -> list[Path]:
    """可能存放 Logs 的目录，按可信度排序。"""
    roots: list[Path] = []
    for p in extra:
        if p:
            roots.append(Path(p))
    for getter in ((running_hearthstone_dir, cached_install_dir) if include_ambient else ()):
        p = getter()
        if p:
            roots.append(p)
            if getter is running_hearthstone_dir:
                remember_install_dir(p)
    roots.extend(
        []
        if not include_ambient
        else [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Blizzard" / "Hearthstone",
            Path(os.environ.get("APPDATA", "")) / "Blizzard" / "Hearthstone",
        ]
    )
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r).lower()
        if not r or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _logs_in(root: Path) -> list[Path]:
    """root 下所有可能的 Logs 目录（含按会话分的子目录）。"""
    logs = root / "Logs"
    out: list[Path] = []
    if (root / "Power.log").exists():
        out.append(root)
    if logs.is_dir():
        out.append(logs)
        try:
            out.extend([p for p in logs.iterdir() if p.is_dir()])
        except OSError:
            pass
    return out


def find_power_log(extra_roots: Iterable[Path] = (), *, include_ambient: bool = True) -> Path | None:
    """在所有候选位置里找最新的 Power.log。"""
    best: tuple[float, Path] | None = None
    for root in candidate_roots(extra_roots, include_ambient=include_ambient):
        for d in _logs_in(root):
            for candidate in [d / "Power.log", *d.glob("Power*.log")]:
                try:
                    mtime = candidate.stat().st_mtime
                except OSError:
                    continue
                if best is None or mtime > best[0]:
                    best = (mtime, candidate)
    return best[1] if best else None


def log_config_targets(extra_roots: Iterable[Path] = (), *, include_ambient: bool = True) -> list[Path]:
    """应该把 log.config 写到哪些目录。

    只写「看起来真的是炉石目录」的地方：有 Hearthstone.exe 或 Hearthstone_Data。
    避免往用户随便给的路径里乱丢文件。
    """
    out: list[Path] = []
    for root in candidate_roots(extra_roots, include_ambient=include_ambient):
        if not root or not root.is_dir():
            continue
        looks_like_hs = (root / "Hearthstone.exe").exists() or (root / "Hearthstone_Data").exists()
        if not looks_like_hs:
            continue
        if root not in out:
            out.append(root)
    return out


def setup_log_config(extra_roots: Iterable[Path] = (), *, dry_run: bool = False,
                     include_ambient: bool = True) -> list[tuple[Path, str]]:
    """写 log.config。返回 [(路径, 结果说明)]。

    已经存在且内容一致就不动；存在但不同就备份成 log.config.bak 再覆盖，
    因为老配置里可能有用户自己开的其它日志类别。
    """
    results: list[tuple[Path, str]] = []
    for root in log_config_targets(extra_roots, include_ambient=include_ambient):
        target = root / "log.config"
        try:
            if target.exists():
                existing = target.read_text(encoding="utf-8", errors="replace")
                if "[Power]" in existing and "LogLevel=1" in existing:
                    results.append((target, "已开启，未改动"))
                    continue
                if not dry_run:
                    backup = root / "log.config.bak"
                    if not backup.exists():
                        backup.write_text(existing, encoding="utf-8")
                if not dry_run:
                    target.write_text(LOG_CONFIG_BODY, encoding="utf-8")
                results.append((target, "已覆盖并备份为 log.config.bak"))
            else:
                if not dry_run:
                    target.write_text(LOG_CONFIG_BODY, encoding="utf-8")
                results.append((target, "已创建"))
        except OSError as exc:
            results.append((target, f"写入失败：{exc}"))
    return results


def tail_start_offset(path: Path, limit: int = READ_LIMIT) -> int:
    """返回最后一个 CREATE_GAME 所在行的起点。

    按块向前扫，直到找到为止 —— 单局日志可能远大于窗口（几 MB），
    只扫最后一段会找不到锚点，于是从半局开始解析（没有玩家实体），
    界面就永远显示「回合判断中」。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    window = max(limit, 1)
    end = size
    while end > 0:
        start = max(0, end - window)
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                chunk = fh.read(end - start)
        except OSError:
            return size
        idx = chunk.rfind(b"CREATE_GAME")
        if idx >= 0:
            line_start = chunk.rfind(b"\n", 0, idx)
            return start + (line_start + 1 if line_start >= 0 else idx)
        if start == 0:
            break
        end = start + 32          # 稍微重叠，避免把一个标记切断
    return 0


class PowerLogWatcher:
    """把「文件增长」变成「状态更新」。"""

    def __init__(self, path: Path, cards: CardDB | None = None, *, follow_from_end: bool = False) -> None:
        self.path = path
        self.cards = cards
        self.state = GameState(cards)
        self._offset = 0
        self._pending: int | None = None
        self._started = False
        self._follow_from_end = follow_from_end
        self.generation = 0     # 每开新局 +1
        self._rearm = False     # 跳过「已结束的上一局」后，等新数据出现再重新定位
        self.version = 0        # 每处理一批 +1

    # ------------------------------------------------------------ 位置管理
    def _init_offset(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            size = self.path.stat().st_size
        except OSError:
            self._offset = 0
            return
        if self._follow_from_end:
            self._offset = size
            return
        start = tail_start_offset(self.path)
        # 大文件里找不到 CREATE_GAME（例如日志被截断/滚过）时，绝不要从头重放——
        # 那会把好几局旧对局重新演一遍，用户重启后看到的就是上一局。
        self._offset = start

    def reset(self) -> None:
        self.state = GameState(self.cards)
        self._pending = None
        self.generation += 1

    # ------------------------------------------------------------ 读取
    def poll(self) -> bool:
        """读新内容。返回 True 表示状态变了。"""
        first = not self._started
        self._init_offset()
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size < self._offset:      # 文件被重建
            self._offset = 0
            self.reset()
        if size == self._offset:
            return False
        if self._rearm:
            # 新对局开始了：重新定位到最后一个 CREATE_GAME，否则会从半局开始解析，
            # 拿不到玩家实体，界面就一直是空的
            self._offset = tail_start_offset(self.path)
            self._rearm = False
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(min(size - self._offset, 8 * 1024 * 1024))
        except OSError:
            return False
        self._offset += len(chunk)
        # 只处理完整的行，半行留到下次（否则中文/JSON 会被截断成乱码）
        cut = chunk.rfind(b"\n")
        if cut < 0:
            self._offset -= len(chunk)
            return False
        if cut + 1 < len(chunk):
            self._offset -= len(chunk) - (cut + 1)
            chunk = chunk[: cut + 1]
        text = chunk.decode("utf-8", errors="replace")

        changed = False
        for line in text.splitlines():
            ev = powerlog.parse_line(line, self._pending)
            if ev is None:
                continue
            if ev.kind == "create_game":
                self.reset()
            elif ev.kind in ("full_entity", "entity", "block_start", "show_entity", "change_entity"):
                if ev.data.get("entity"):
                    self._pending = ev.data["entity"]
            self.state.apply(ev)
            changed = True
        if first and self.state.game_over:
            # 启动时读到的最后一局已经打完了：不要把它当成“正在进行的对局”，
            # 直接把读指针推到文件末尾，等下一局开始。
            self.reset()
            try:
                self._offset = self.path.stat().st_size
                self._rearm = True
            except OSError:
                pass
            return False
        if changed:
            self.version += 1
        return changed

