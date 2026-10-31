"""实战复盘：把「这一局哪里能做得更好」变成可验证的事实。

只做第一层——**事实层**。不猜你当时在想什么，只比对日志里确定发生过的事：

  · 有斩杀没斩：当回合最大打脸伤害够杀死对面，但回合结束时对面还活着
  · 手牌溢出：手牌 10 张时还在过牌（会烧牌）

为什么不做「建议采纳率」和「自动调权重」：样本太少时那是拟合噪声，
而且你的操作不等于最优，拿它训练只会把现有习惯固化。先让事实说话。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .cards import DATA_DIR

GAMES_PATH = DATA_DIR / "games.jsonl"




@dataclass
class TurnRecord:
    turn: int
    lethal_available: bool = False
    lethal_damage: int = 0
    mana_left: int = 0
    playable: int = 0
    hand: int = 0
    our_hp: int = 30
    opp_hp: int = 30
    advice: str = ""
    killed: bool = False


@dataclass
class GameRecord:
    ts: str = ""
    result: str = ""
    our_class: str = ""
    opp_class: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "result": self.result, "our_class": self.our_class,
                "opp_class": self.opp_class, "turns": self.turns}


def summarize(game: dict[str, Any]) -> list[str]:
    """把一局记录变成「人话」的结论，最多 5 条。"""
    out: list[str] = []
    turns = game.get("turns") or []
    missed = [t for t in turns if t.get("lethal_available") and not t.get("killed")]
    if missed:
        names = "、".join(f"第 {t['turn']} 回合（{t['lethal_damage']} 点）" for t in missed[:3])
        out.append(f"有斩杀没斩：{names}")

    if not out:
        out.append("没有发现明显的低级失误（没有漏掉斩杀）")
    return out


def append_game(record: GameRecord, path: Path = GAMES_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_games(path: Path = GAMES_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows
