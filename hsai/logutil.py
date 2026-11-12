"""运行时诊断日志：统一写入 `data/hsai.log`，并做简单轮转。

为什么单独一个模块
------------------
这份日志原来由 advisor / state / ui **三处各自 `open(..., "a")` 手写**，
时间戳格式各不相同、没有轮转，而且都靠 `except OSError: pass` 兜底 ——
真出问题时，往往连「日志本身没写进去」都看不出来。

规则
----
· 单文件超过 1 MB 就只保留最后 2000 行。不另建 `.1` 文件，
  因为用户排查时只会去点「详细日志：data\\hsai.log」那一个路径。
· 写日志失败**绝不抛异常**：日志不能把主流程搞崩。
"""

from __future__ import annotations

import os
import time

from .paths import LOG_FILE

MAX_BYTES = 1024 * 1024
KEEP_LINES = 2000


def _rotate_if_needed() -> None:
    try:
        if LOG_FILE.stat().st_size <= MAX_BYTES:
            return
        rows = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        tmp = LOG_FILE.with_suffix(LOG_FILE.suffix + ".tmp")
        tmp.write_text("\n".join(rows[-KEEP_LINES:]) + "\n", encoding="utf-8")
        os.replace(tmp, LOG_FILE)
    except OSError:
        pass


def log(msg: str) -> None:
    """追加一行带时间戳的诊断日志。永不抛异常。"""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass
