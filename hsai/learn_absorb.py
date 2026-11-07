"""learn 的观察吸收 / 原型重建 / 统计。

门面 learn.py 从这里 re-export；子模块之间可互相 import，但不 import learn 门面。
advisor 里 `from . import learn as learn_mod` 在函数内延迟回查 `absorb_observations`，
这里保持该名字可用即可，不改 import 方向。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import meta as meta_mod
from .advisor import OBSERVATIONS_PATH, load_observations
from .cards import CardDB
from .meta import MetaDB, build_from_observations


def _overlap(a, b) -> float:
    ca, cb = set(a.cards), set(b.cards)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / min(len(ca), len(cb))


def absorb_observations(cards: CardDB, meta: MetaDB, *, min_games: int = 2,
                        observations_path: Path = OBSERVATIONS_PATH,
                        stats_out: dict[str, Any] | None = None) -> int:
    """把「打过的对局」变成新卡组原型，返回新增数量。

    这是自动学习的主干：每局结束调一次，够了样本就长出一个本地原型，
    下一局遇到同样的牌就能认出来。和已有的（导入的）原型重合度高就跳过，
    免得同一套牌被反复加进去。
    """
    obs = load_observations(observations_path)
    if stats_out is not None:
        stats_out["observations"] = len(obs)
    if len(obs) < min_games:
        return 0
    learned = build_from_observations(obs, cards, min_games=min_games, threshold=0.34)
    added = 0
    for a in learned:
        if not a.cards:
            continue
        same_class = [b for b in meta if b.card_class == a.card_class]
        if any(_overlap(a, b) >= 0.6 for b in same_class):
            continue
        meta.upsert(
            meta_mod.Archetype(
                # id 跟内容走，不跟「第几个」走 —— 否则同职业多一套牌，
                # 其余原型的 id 全平移，observations 里存的 truth.arch 就对不上了。
                id=f"learned_{a.id}",
                name=a.name,
                card_class=a.card_class,
                prior=max(0.35, a.prior),
                prior_base=max(0.35, a.prior),
                cards=dict(a.cards),
                tags=["本地"],
                source="learned",
                note=a.note,
            )
        )
        added += 1
    return added


def rebuild_learned(cards: CardDB, meta: MetaDB, *, min_games: int = 2,
                    observations_path: Path = OBSERVATIONS_PATH) -> dict[str, Any]:
    """把「学出来的原型」整批丢掉，按当前口径从对局记录重新聚类。

    为什么需要它：模板是从历史 revealed 聚出来的，而历史数据里混着 28% 的
    衍生物（召唤物、幸运币）。采集端修好之后老模板不会自己变干净 —— 只能重建。

    只动 `source == "learned"` 的。导入的卡组（deckcode）是外部输入，
    重建它们等于篡改用户数据。
    """
    removed = [a for a in meta if a.source == "learned"]
    keep = [a for a in meta if a.source != "learned"]
    meta.archetypes = keep
    meta.normalize_priors()
    collected: dict[str, Any] = {}
    added = absorb_observations(cards, meta, min_games=min_games,
                                observations_path=observations_path,
                                stats_out=collected)
    return {
        "removed": [(a.id, a.name, a.total()) for a in removed],
        "added": added,
        "observations": collected.get("observations", 0),
    }


def stats(observations_path: Path = OBSERVATIONS_PATH) -> dict[str, Any]:
    obs = load_observations(observations_path)
    if not obs:
        return {"games": 0}
    wins = sum(1 for o in obs if o.get("result") == "win")
    by_class: dict[str, dict[str, int]] = {}
    for o in obs:
        cls = o.get("opp_class") or "?"
        row = by_class.setdefault(cls, {"games": 0, "wins": 0})
        row["games"] += 1
        row["wins"] += 1 if o.get("result") == "win" else 0
    return {
        "games": len(obs),
        "winrate": wins / len(obs),
        "by_class": {
            k: {"games": v["games"], "winrate": v["wins"] / max(1, v["games"])}
            for k, v in sorted(by_class.items(), key=lambda kv: -kv[1]["games"])
        },
    }
