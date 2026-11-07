"""meta 的命名 / 相似度纯函数。

门面 meta.py 从这里 re-export；子模块不 import meta 门面。
"""

from __future__ import annotations

import hashlib

from .cards import CLASS_CN, CardDB

# 稀有度档次：数字大的优先。命名是**硬优先级**——有传说就用传说，
# 不管那张传说出现率多低、费用多便宜。
RARITY_RANK = {
    "LEGENDARY": 3,
    "EPIC": 2,
    "RARE": 1,
    "COMMON": 0,
    "FREE": -1,      # 基本牌没有稀有度，排在普通之下
}

def _copy_ratio(count: int, card) -> float:
    """张数 -> 「满编程度」0..1，用作没有真实出牌率时的替代。

    传说牌上限就是 1 张，不能因为只有 1 张就被判成边缘牌 —— 否则每套牌的
    传说牌都会被扣一半分，正好把最该当招牌的那张排掉。
    """
    cap = 1 if (card is not None and (getattr(card, "rarity", "") or "").upper() == "LEGENDARY") else 2
    return min(1.0, max(0.0, count / cap))

def key_card_rank(card, presence: float) -> tuple:
    """命名用的排序键：**先比稀有度，再比出牌率**。

    为什么不用加权乘积：乘积会把两件事混成一个数，于是「15 费的史诗」能压过
    「3 费的传说」—— 但用户要的规则是「有传说就用传说」，这是一条硬优先级，
    不该被费用乘数稀释。费用只用来打破「稀有度和出牌率都完全相同」的死局。

    返回元组而不是分数，是因为「先按 A 再按 B」本来就是字典序，写成乘积
    还得挑权重去近似它，纯属自找麻烦。
    """
    rarity = RARITY_RANK.get((getattr(card, "rarity", "") or "").upper(), 0)
    cost = getattr(card, "cost", 0)
    cost = cost if isinstance(cost, int) else 0
    return (-rarity,
            -round(presence, 6),
            -cost,                       # 仅用于打破完全相同的死局
            getattr(card, "name", "") or "",
            getattr(card, "id", "") or "")   # id 兜底，保证可复现

def pick_key_cards(deck: dict[str, int], cards: CardDB, *,
                   presence: dict[str, float] | None = None,
                   limit: int = 3) -> list[str]:
    """挑出最能代表这套牌的几张牌，最像的排在最前（返回 card_id）。

    排序规则见 `key_card_rank`：稀有度优先，同稀有度比出牌率。

    presence 不给时按张数推（满编 2 张 = 1.0）。聚类那边有真实出牌率，直接用
    真实值 —— 张数是从出牌率反推的，比它粗。
    """
    ranked = []
    for cid, count in deck.items():
        card = cards.get(cid) if cards else None
        if card is None:
            continue
        ratio = presence[cid] if presence and cid in presence else _copy_ratio(count, card)
        ranked.append((key_card_rank(card, ratio), card))
    # 排序键自带名称和 id 兜底，所以同一份数据每次排出来逐字一致 ——
    # 否则两次重建名字会变，而 opp_history 是按名字匹配的，名字一变先验就断档。
    ranked.sort(key=lambda p: p[0])
    return [c.id for _, c in ranked[:limit]]

def content_id(card_class: str, cards: dict[str, int]) -> str:
    """由标志牌集合算出稳定 id（8 位十六进制）。

    为什么不用「同职业第几个」：那个编号会随原型库的增删整体平移 ——
    删掉一个战士原型，`learned_warrior_2` 就变成 `learned_warrior_0`。
    而 `observations.jsonl` 的 `truth.arch` 是按 id 记的，历史真值当场对不上，
    先验校准会把那些局算成「这套牌从没出现过」。

    用内容哈希后，只要这套牌还是这套牌，id 就不变。
    """
    payload = "|".join(f"{cid}:{n}" for cid, n in sorted(cards.items()))
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:8]

def _name_archetype(cls: str, keys: list[str], cards: CardDB, used: set[str]) -> str:
    """`职业·关键牌`，**只取一张牌的名字**。

    撞名时不是把两张拼起来（`战士·A/B`），而是往后挪一张候选牌 —— 名字要能
    一眼读完，拼接只会让列表里每行都变长，反而更难扫。
    """
    prefix = CLASS_CN.get(cls, cls)
    for cid in keys:
        name = f"{prefix}·{cards.display_name(cid)}"
        if name not in used:
            return name
    # 候选牌全撞了（同一个职业里两套牌共用全部招牌牌）—— 加序号保证唯一，
    # 名字重复会让 opp_history 的先验记到两套牌头上。
    base = f"{prefix}·{cards.display_name(keys[0]) if keys else '新形态'}"
    i = 2
    while f"{base}#{i}" in used:
        i += 1
    return f"{base}#{i}"

def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
