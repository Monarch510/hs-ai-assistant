"""learn 的自对弈胜率优化（镜像对局 + 坐标轮换爬山）。

门面 learn.py 从这里 re-export；子模块之间可互相 import，但不 import learn 门面。
"""

from __future__ import annotations

import math
import random
from typing import Callable, Iterable

from . import mulligan as mulligan_mod
from . import sim
from .cards import CardDB
from .meta import MetaDB
from .model import DEFAULT_WEIGHTS
from .learn_weights import Report, load_mulligan_weights, load_weights


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
