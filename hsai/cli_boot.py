"""命令行启动底座：控制台编码、卡牌库加载、pythonw 的无控制台兜底。

单独成模块是因为所有 cli_* 命令族都要用 _db()：
如果它们从 cli.py 取，而 cli.py 又要 import 命令族，就成环了。
"""

from __future__ import annotations

import sys

from . import cards as cards_mod
from .cards import CardDB
from .paths import LOG_FILE

LOG_PATH = LOG_FILE


def _ensure_console() -> None:
    """让中文能打印出来，但不硬把编码改成 UTF-8。

    Windows 控制台常见两种：代码页 936（GBK，能直接显示中文）和 65001（UTF-8）。
    如果当前编码已经能表示中文就别动它 —— 强行 reconfigure(utf-8) 反而会让
    936 控制台显示成乱码。只有真的编码不了中文时（例如 cp1252）才切到 UTF-8。
    """
    try:
        enc = (sys.stdout.encoding or "").lower()
        "中文".encode(enc or "utf-8")
    except (LookupError, UnicodeEncodeError):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        return
    # 能编码中文不代表能编码所有符号（如 ⚠），放宽错误策略即可，别动编码
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass


def _db() -> CardDB:
    db = cards_mod.load()
    if not len(db):
        print("卡牌库为空：请先运行 `python -m hsai cards --update`")
        raise SystemExit(2)
    return db


def _fix_streams() -> None:
    """pythonw 启动时没有控制台，sys.stdout/stderr 可能是 None。

    那样任何一个 print 都会抛 AttributeError —— 表现为「双击后什么都没出现」。
    这里把它们接到日志文件上，既不会崩，也留下了排查线索。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    except OSError:
        import os

        fh = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh
