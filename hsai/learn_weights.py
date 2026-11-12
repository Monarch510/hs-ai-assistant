"""learn 的权重读写与优化报告。

门面 learn.py 从这里 re-export；子模块不 import learn 门面。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import mulligan as mulligan_mod
from .advisor import MULLIGAN_WEIGHTS_PATH, WEIGHTS_PATH
from .model import DEFAULT_WEIGHTS


@dataclass
class Report:
    baseline: float = 0.5
    best: float = 0.5
    rounds: int = 0
    accepts: list[str] = field(default_factory=list)
    games: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline_winrate": round(self.baseline, 3),
            "best_winrate": round(self.best, 3),
            "rounds": self.rounds,
            "accepted_changes": self.accepts,
            "games_played": self.games,
        }


def load_weights(path: Path = WEIGHTS_PATH) -> dict[str, float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {**DEFAULT_WEIGHTS, **(payload.get("weights") or {})}
    except (OSError, ValueError):
        return dict(DEFAULT_WEIGHTS)


def save_weights(weights: dict[str, float], path: Path = WEIGHTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"weights": weights, "note": "由 hsai learn 自对弈生成"}, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def load_mulligan_weights(path: Path = MULLIGAN_WEIGHTS_PATH) -> dict[str, float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {**mulligan_mod.DEFAULT_WEIGHTS, **(payload.get("weights") or {})}
    except (OSError, ValueError):
        return dict(mulligan_mod.DEFAULT_WEIGHTS)


def save_mulligan_weights(weights: dict[str, float], path: Path = MULLIGAN_WEIGHTS_PATH) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # 只换权重，别把用户手写的 note 或其它键冲掉
    payload["weights"] = weights
    payload.setdefault("note", "由 hsai learn 自对弈生成")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
