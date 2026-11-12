"""窗口状态的读写：从 ui.py 拆出的模块级函数，门面会 re-export。

★ 这些函数读的 STATE_PATH / DATA_DIR 是 ui 模块上「会被测试赋值」的名字：
  必须函数内 `from . import ui` 回查，不能在 import 时把值拷进来，否则
  tests/_ui_support.py 里 `ui.STATE_PATH = 临时目录/window.json` 就静默失效，
  测试会把窗口摆位写回真实 data/。
"""

from __future__ import annotations

import json
import math
import os
from typing import Any


def _load_state() -> dict[str, Any]:
    from . import ui
    try:
        data = json.loads(ui.STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _as_int(value: Any, default: int, lo: int | None = None, hi: int | None = None) -> int:
    # window.json 是用户可改的文件，坏值降级成默认，别让整窗起不来
    try:
        v = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _as_float(value: Any, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(v):
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _as_set(value: Any) -> set[str]:
    # 字符串/数字不能拿 set() 拆成字符，只认真正的序列
    return set(value) if isinstance(value, (list, tuple, set)) else set()


def _save_state(data: dict[str, Any]) -> None:
    from . import ui
    try:
        ui.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ui.STATE_PATH.with_suffix(ui.STATE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, ui.STATE_PATH)
    except OSError as exc:
        from . import logutil
        logutil.log(f"window.json 写入失败：{exc}")
