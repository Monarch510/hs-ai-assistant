"""按「我方职业 × 对手形态」记忆权重修正。

为什么按对局分：打快攻该压低打脸、提高解场；打控制反过来。混在一起学，
两边的信号互相抵消，最后什么都没学到。

为什么存原始样本而不是平均值：
    - 中位数抗噪（一次手滑不会带偏）
    - 样本少于 3 次不生效，避免用一两局就改默认行为
    - 方向不一致时（一半高一半低）自动退回 1.0，宁可不改
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import Any

from .cards import DATA_DIR

PATH = DATA_DIR / "weights_by_matchup.json"
MIN_SAMPLES = 3
MAX_SAMPLES = 12
LIMIT = (0.5, 2.0)


def _load() -> dict[str, Any]:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any]) -> None:
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def key(our_class: str, archetype: str) -> str:
    return f"{our_class or '?'}|{archetype or '?'}"


def record(our_class: str, archetype: str, weights: dict[str, float]) -> None:
    """记下一次用户的纠正（来自输入框）。"""
    if not weights:
        return
    data = _load()
    slot = data.setdefault(key(our_class, archetype), {})
    for name, val in weights.items():
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if not (LIMIT[0] <= num <= LIMIT[1]):
            continue
        samples = slot.setdefault(name, [])
        samples.append(round(num, 2))
        slot[name] = samples[-MAX_SAMPLES:]
    _save(data)


def resolve(our_class: str, archetype: str) -> dict[str, float]:
    """取出该对局当前生效的权重倍数。样本不足或方向不一致时返回空。"""
    slot = _load().get(key(our_class, archetype)) or {}
    out: dict[str, float] = {}
    for name, samples in slot.items():
        nums = [float(x) for x in samples if isinstance(x, (int, float))]
        if len(nums) < MIN_SAMPLES:
            continue
        # 方向不一致（有的调高有的调低）就放弃，说明这条规律不成立
        if all(n > 1.0 for n in nums) or all(n < 1.0 for n in nums):
            out[name] = round(median(nums), 2)
    return out
