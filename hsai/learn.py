"""自主学习。

这里没有任何魔法，只有三件真事：

1. **对局记录**：每局结束把「对手职业 / 已暴露的牌 / 我们的胜负」写进
   `data/observations.jsonl`。用得越多，本地数据越厚。
   注意这里存的是**预测和真值两份**：预测只用来算准确率，学习只吃真值
   （`infer.retrospect()` 局末回看的那份）。拿预测当真值会让先验自我确认。
2. **从自己的对局里自举环境**：`build_local_meta` 把观察到的牌集合聚类成
   卡组原型（见 meta.build_from_observations），不需要任何外部网站。
3. **权重自对弈优化**：在同一套牌（镜像对局）里让「候选权重」对打「基准权重」，
   胜率高就采纳。这是坐标轮换爬山，不是强化学习，但它是真在优化的闭环。

必须说清楚的边界：第 3 步优化的是**本仓库模拟器里的胜率**，模拟器对卡牌效果
只建模了常见子集。所以它调出来的权重是「在这个近似环境里更能赢」，
不等于天梯胜率提升。它的价值在于：让评估函数的比例至少在内部一致、可复现，
而不是靠拍脑袋。
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import mulligan as mulligan_mod
from . import meta as meta_mod
from . import sim
from .advisor import MULLIGAN_WEIGHTS_PATH, OBSERVATIONS_PATH, WEIGHTS_PATH, load_observations
from .cards import CardDB
from .meta import MetaDB, build_from_observations
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"weights": weights, "note": "由 hsai learn 自对弈生成"}, ensure_ascii=False, indent=1),
                    encoding="utf-8")


# ---------------------------------------------------------------- 自举环境

def build_local_meta(cards: CardDB, *, min_games: int = 3, threshold: float = 0.34,
                     observations_path: Path = OBSERVATIONS_PATH) -> tuple[MetaDB, int]:
    obs = load_observations(observations_path)
    db = build_from_observations(obs, cards, min_games=min_games, threshold=threshold)
    return db, len(obs)


def calibrate_priors(meta: MetaDB, cards: CardDB, observations_path: Path = OBSERVATIONS_PATH) -> int:
    """按「你实际遇到的比例」修正先验：环境是本地化的。

    只统计**真值可信**的局（`truth.trusted`）。以前统计的是 `opp_archetype`，
    那是当时的预测值 —— 预测偏哪套，先验就往哪套偏，越用越偏。
    老版本写的行没有 truth 字段，一律跳过：宁可少学，不拿猜测训练。
    """
    obs = load_observations(observations_path)
    if not obs:
        return 0
    counts: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for row in obs:
        cls = (row.get("opp_class") or "").upper()
        truth = row.get("truth") or {}
        if not cls or not truth.get("trusted"):
            continue
        arch = truth.get("arch") or ""
        by_class[cls] = by_class.get(cls, 0) + 1
        if arch:
            counts[arch] = counts.get(arch, 0) + 1

    updated = 0
    for arch in meta:
        total = by_class.get(arch.card_class, 0)
        if total < 5:
            continue
        seen = counts.get(arch.id, 0) / total
        # 与先验做一次混合，样本少时不要剧烈跳变
        weight = min(0.7, total / 20)
        arch.prior = (1 - weight) * arch.prior + weight * max(seen, 0.02)
        updated += 1
    meta.normalize_priors()
    return updated


# ---------------------------------------------------------------- 自对弈优化

def mirror_winrate(
    cards: CardDB,
    meta: MetaDB,
    candidate: dict[str, float],
    baseline: dict[str, float],
    *,
    archetype_ids: Iterable[str] | None = None,
    games: int = 4,
    mulligan_weights: dict[str, float] | None = None,
    seed: int = 0,
) -> tuple[float, int]:
    """同一套牌，候选权重 vs 基准权重。返回 (候选胜率, 局数)。

    每一对局都打两次并互换席位：候选先当「我方」再当「对方」。
    不这样做的话，引擎里任何一点点席位偏置（先手/枚举顺序）都会被算成
    「权重更好」，优化器就会去学一个假信号。
    """
    ids = list(archetype_ids) if archetype_ids else [a.id for a in meta if len(a.cards) >= 4][:6]
    rng = random.Random(seed)
    wins = 0
    decided = 0
    for i, aid in enumerate(ids):
        arch = meta.get(aid)
        if arch is None:
            continue
        deck = sim.make_deck(arch, cards, rng)
        for g in range(games):
            base_seed = seed + i * 131 + g * 7
            for swap in (False, True):
                wa, wb = (baseline, candidate) if swap else (candidate, baseline)
                out = sim.run_game(
                    deck, deck, cards,
                    weights_a=wa, weights_b=wb,
                    mulligan_weights=mulligan_weights,
                    seed=base_seed + (1 if swap else 0),
                    class_a=arch.card_class, class_b=arch.card_class,
                )
                if out.winner is None:
                    continue
                decided += 1
                winner_is_candidate = (out.winner == 1) if swap else (out.winner == 0)
                if winner_is_candidate:
                    wins += 1
    return (wins / decided if decided else 0.5), decided


def optimize(
    cards: CardDB,
    meta: MetaDB,
    *,
    keys: Iterable[str] | None = None,
    games: int = 4,
    rounds: int = 2,
    step: float = 0.4,
    seed: int = 0,
    mulligan_weights: dict[str, float] | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, float], Report]:
    """坐标轮换爬山。

    用**乘法步长**而不是加减固定值：贪心策略对权重的响应是分段常数的，
    加减 0.22 往往一步都走不动（界面完全一样），乘 0.6/1.4 才真的会改变选牌顺序。
    实践里最能改变行为的就是 tempo / aoe_risk 这类比例项。
    """
    say = progress or (lambda _m: None)
    baseline = dict(DEFAULT_WEIGHTS)
    best = load_weights()
    best_wr, n = mirror_winrate(cards, meta, best, baseline, games=games,
                               mulligan_weights=mulligan_weights, seed=seed)
    report = Report(baseline=0.5, best=best_wr, games=n)
    say(f"初始：候选 {best_wr:.1%} vs 基准（{n} 局）")

    keys = list(keys or [k for k in DEFAULT_WEIGHTS])
    # 这里的胜率是确定性的（固定种子 + 固定搜索），所以门槛可以放低；
    # 代价是容易过拟合这几十局，换一版 --seed 才算真验证。
    threshold = 0.035
    for r in range(rounds):
        improved = False
        for k in keys:
            cur = best.get(k, DEFAULT_WEIGHTS.get(k, 1.0))
            for delta in (step, -step):
                cand = dict(best)
                cand[k] = round(max(0.02, cur * (1 + delta)), 3)
                if cand[k] == cur:
                    continue
                wr, n2 = mirror_winrate(cards, meta, cand, baseline, games=games,
                                        mulligan_weights=mulligan_weights, seed=seed + r + 1)
                report.games += n2
                if wr > best_wr + threshold:
                    say(f"  {k}: {cur} -> {cand[k]}，胜率 {best_wr:.1%} -> {wr:.1%} ✓")
                    best, best_wr = cand, wr
                    report.accepts.append(f"{k}:{cur}->{cand[k]}")
                    improved = True
                    break
        report.rounds = r + 1
        if not improved:
            say("  本轮没有找到超过门槛的改进（这在离散策略下很常见）")
            break
    report.best = best_wr
    return best, report


def optimize_mulligan(
    cards: CardDB,
    meta: MetaDB,
    *,
    policy_weights: dict[str, float] | None = None,
    keys: Iterable[str] | None = None,
    games: int = 4,
    rounds: int = 1,
    step: float = 3.0,
    seed: int = 7,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, float], Report]:
    """留牌权重单独优化：目标仍是镜像对局胜率。"""
    say = progress or (lambda _m: None)
    base = dict(mulligan_mod.DEFAULT_WEIGHTS)
    best = load_mulligan_weights()
    pw = policy_weights or load_weights()

    def measure(mw: dict[str, float]) -> tuple[float, int]:
        return mirror_winrate(cards, meta, pw, dict(DEFAULT_WEIGHTS), games=games,
                              mulligan_weights=mw, seed=seed)

    best_wr, n = measure(best)
    report = Report(games=n, best=best_wr)
    say(f"留牌权重初始胜率：{best_wr:.1%}（{n} 局）")
    keys = list(keys or ["vs_aggro_expensive", "vs_aggro_cheap", "keep_aoe_vs_aggro",
                         "duplicate_penalty", "cost_1", "cost_4"])
    for r in range(rounds):
        improved = False
        threshold = max(0.03, 1.2 * math.sqrt(0.25 / max(report.games, 1)))
        for k in keys:
            for delta in (step, -step):
                cand = dict(best)
                cand[k] = round(cand.get(k, 0.0) + delta, 2)
                wr, n2 = measure(cand)
                report.games += n2
                if wr > best_wr + threshold:
                    say(f"  {k}: {best.get(k)} -> {cand[k]}，胜率 {best_wr:.1%} -> {wr:.1%} ✓")
                    best, best_wr = cand, wr
                    report.accepts.append(f"{k}={cand[k]}")
                    improved = True
                    break
        report.rounds = r + 1
        if not improved:
            break
    report.best = best_wr
    return best, report


def _overlap(a, b) -> float:
    ca, cb = set(a.cards), set(b.cards)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / min(len(ca), len(cb))


def absorb_observations(cards: CardDB, meta: MetaDB, *, min_games: int = 2,
                        observations_path: Path = OBSERVATIONS_PATH) -> int:
    """把「打过的对局」变成新卡组原型，返回新增数量。

    这是自动学习的主干：每局结束调一次，够了样本就长出一个本地原型，
    下一局遇到同样的牌就能认出来。和已有的（导入/种子）原型重合度高就跳过，
    免得同一套牌被反复加进去。
    """
    obs = load_observations(observations_path)
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
                id=f"learned_{a.card_class.lower()}_{len(same_class)}",
                name=a.name,
                card_class=a.card_class,
                prior=max(0.35, a.prior),
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

    只动 `source == "learned"` 的。导入的卡组（deckcode）和内置种子（seed）
    是外部输入，重建它们等于篡改用户数据。
    """
    removed = [a for a in meta if a.source == "learned"]
    keep = [a for a in meta if a.source != "learned"]
    meta.archetypes = keep
    meta.normalize_priors()
    added = absorb_observations(cards, meta, min_games=min_games,
                                observations_path=observations_path)
    return {
        "removed": [(a.id, a.name, a.total()) for a in removed],
        "added": added,
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

