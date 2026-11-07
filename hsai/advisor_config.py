"""advisor 的配置小工具：读 json、比例值兜底。

零状态，只被 Advisor.__init__ 用。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def _positive_scale(value: Any, default: float) -> float:
    """配置里的缩放系数只认有限正数；坏值/缺失一律回退模块常量。

    手写文件里写错一个 0 或负数，会让留牌/出牌的低权重整项消失或反号 ——
    这种错误没有任何报错，只能在这里挡住。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = float(value)
    return value if math.isfinite(value) and value > 0 else default
