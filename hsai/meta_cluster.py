"""meta 的自举聚类 / 导入。

门面 meta.py 保留 MetaDB / _load_dir；这里只放两个构建函数。
★ 子模块不顶层 import meta 门面：MetaDB 定义在门面上，函数内延迟回查避免成环。
"""

from __future__ import annotations

from typing import Any

from .cards import CardDB
from .meta_types import DECK_MAX_CARDS, Archetype, _resolve_cards
from .meta_naming import _jaccard, _name_archetype, content_id, pick_key_cards


def build_from_observations(
    observations: list[dict[str, Any]],
    cards: CardDB,
    *,
    min_games: int = 3,
    threshold: float = 0.34,
) -> "MetaDB":
    """从「你自己的对局记录」里聚类出本地环境卡组。

    observations: [{"opp_class": "WARRIOR", "revealed": ["CORE_EX1_604", ...]}, ...]
    只用已经观测到的牌算 Jaccard 相似度，按「与簇内样本的平均相似度」凝聚聚类。
    这一个函数就是「不依赖任何外部网站也能跟上环境」的实现。
    """
    # MetaDB 定义在门面 meta.py 上，顶层 import 会成环；函数内延迟回查。
    from . import meta as _meta

    groups_by_class: dict[str, list[set[str]]] = {}
    for obs in observations:
        cls = (obs.get("opp_class") or "").upper()
        # schema 2 之后 revealed 是 [[card_id, 回合], ...]，老数据是 [card_id, ...]，
        # 两种都要能读 —— 否则升级一次就把历史样本全废了。
        #
        # 衍生物必须在这里滤掉，不能只靠采集端：老数据里召唤物和幸运币占了
        # 28%，它们会进「出现率 >= 50% 就是标志牌」的统计，把原型撑到 40 张。
        revealed: set[str] = set()
        for item in (obs.get("revealed") or []):
            cid = item[0] if isinstance(item, (list, tuple)) else item
            if cid and not cards.is_token(str(cid)):
                revealed.add(str(cid))
        if not cls or len(revealed) < 2:
            continue
        groups_by_class.setdefault(cls, []).append(revealed)

    archetypes: list[Archetype] = []
    used_names: set[str] = set()
    for cls, samples in groups_by_class.items():
        clusters: list[list[set[str]]] = []
        for sample in samples:
            best_i, best_sim = -1, 0.0
            for i, cluster in enumerate(clusters):
                # 单链会让 A~B、B~C 但 A≁C 的三套牌串成一簇，签名只剩中间那套的牌；
                # 与簇内所有样本取平均，B 就当不成桥了。
                sim = sum(_jaccard(sample, other) for other in cluster) / len(cluster)
                if sim > best_sim:
                    best_i, best_sim = i, sim
            if best_i >= 0 and best_sim >= threshold:
                clusters[best_i].append(sample)
            else:
                clusters.append([sample])
        clusters.sort(key=len, reverse=True)
        total = sum(len(c) for c in clusters) or 1
        for cluster in clusters:
            if len(cluster) < min_games:
                continue
            freq: dict[str, int] = {}
            for sample in cluster:
                for cid in sample:
                    freq[cid] = freq.get(cid, 0) + 1
            # 出现率 >= 50% 视为标志牌；但「幸运币」这种人人可能有的牌要排除，
            # 否则学出来的原型名字会变成「死亡骑士·战场通灵师/幸运币」这种怪东西
            noisy = {"GAME_005"}
            signature = {cid: min(2, max(1, round(n / len(cluster) * 2))) for cid, n in freq.items()
                         if n / len(cluster) >= 0.5 and cid not in noisy}
            if not signature:
                continue
            # 牌数超过合法卡组上限，说明这个簇其实是几套不同的牌被串到了一起，
            # 或者把衍生物也数了进去。留着它只会污染判断，不如整簇放弃。
            if sum(signature.values()) > DECK_MAX_CARDS:
                continue
            # 用关键牌命名，不是「出牌率最高的牌」：出牌率最高的是 1 费填充牌，
            # 那样会得到「战士·斩杀」这种看不出是哪套牌的名字。
            # 多取几个候选（5 个），撞名时往后挪，而不是把两张名字拼起来。
            keys = pick_key_cards(
                signature, cards, limit=5,
                presence={cid: n / len(cluster) for cid, n in freq.items() if cid in signature},
            )
            name = _name_archetype(cls, keys, cards, used_names)
            used_names.add(name)
            archetypes.append(
                Archetype(
                    id=f"{cls.lower()}_{content_id(cls, signature)}",
                    name=name,
                    card_class=cls,
                    prior=len(cluster) / total,
                    prior_base=len(cluster) / total,
                    cards=signature,
                    tags=["本地环境"],
                    source="bootstrap",
                    note=f"{len(cluster)} 局样本聚类",
                )
            )

    if not archetypes:
        return _meta.MetaDB([], cards)
    return _meta.MetaDB(archetypes, cards)


def import_archetypes(payload: dict[str, Any], cards: CardDB, db: "MetaDB | None" = None) -> "MetaDB":
    """导入外部卡组表（例如从第三方站点导出的 json）。

    payload 形如：
        {"archetypes": [{"name": "节奏战", "class": "WARRIOR", "share": 12.5,
                         "cards": {"血帆海盗": 2, ...}}]}
    share 是使用率百分比，会被归一化成先验。
    """
    # 同上：延迟回查门面上的 MetaDB，避免子模块顶层 import 门面成环。
    from . import meta as _meta

    db = db or _meta.MetaDB([], cards)
    incoming = payload.get("archetypes") or []
    total_share = sum(float(a.get("share", 1.0)) for a in incoming) or 1.0
    for i, a in enumerate(incoming):
        resolved = _resolve_cards(a.get("cards") or {}, cards)
        if not resolved:
            continue
        cls = (a.get("class") or "NEUTRAL").upper()
        db.upsert(
            Archetype(
                id=a.get("id") or f"import_{i}",
                name=a.get("name") or f"导入卡组 {i}",
                card_class=cls,
                prior=float(a.get("share", 1.0)) / total_share,
                prior_base=float(a.get("share", 1.0)) / total_share,
                cards=resolved,
                tags=list(a.get("tags") or []),
                source="import",
            )
        )
    db.normalize_priors()
    return db
