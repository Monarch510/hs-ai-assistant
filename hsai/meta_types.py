"""meta 的原型数据类与解析/序列化纯函数。

门面 meta.py 保留 MetaDB / _load_dir；这里只放 Archetype 及其读写辅助。
子模块不 import meta 门面。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .cards import CLASS_CN, CardDB

# 一副合法卡组最多 40 张（雷诺/40 张模式）。超过这个数的原型不是「一套牌」，
# 多半是两套牌被聚类合并了，或者把衍生物当成了卡组牌 —— 这种模板会靠
# 「牌多所以解释得多」赢下覆盖率比较，不该参与判断。
DECK_MAX_CARDS = 40


# 卡牌 id 形如 CORE_CS2_029 / NEW1_025，用来区分「这是 id 还是中文名」
@dataclass
class Archetype:
    id: str
    name: str
    card_class: str
    prior: float
    cards: dict[str, int] = field(default_factory=dict)  # card_id -> 张数
    tags: list[str] = field(default_factory=list)
    source: str = "import"
    note: str = ""
    # 看到「不在这套牌里」的牌时给多大概率。普通卡组极低（几乎不可能），
    # 但「备阵/偷牌/复制」类卡组会真的打出对手的牌，必须放宽，否则会被误杀。
    unexpected: float = 1e-4
    # 校准前的先验。`meta calibrate` 每次都从它重新混合，而不是在当前 prior 上
    # 再混一次 —— 否则同一个命令跑三遍，先验会被逐步「洗」成观测频率。
    # None 表示老文件没这个字段，退化成用当前的 prior。
    prior_base: float | None = None

    @property
    def class_cn(self) -> str:
        return CLASS_CN.get(self.card_class, self.card_class)

    @property
    def base_prior(self) -> float:
        """校准的起点：导入/聚类时给的原始先验。"""
        return self.prior if self.prior_base is None else self.prior_base

    def total(self) -> int:
        return sum(self.cards.values())

    def as_dict(self, cards: CardDB | None = None) -> dict[str, Any]:
        """写成能直接读的文件。

        `cards` 写成带名称和费用的数组并按费用升序 —— 光看 id 和数字没法核，
        而费用顺序正好是出牌的先后顺序，扫一眼就知道这套牌怎么展开。
        没有 CardDB 时退化成只有 id 和 count（依然合法，能再读回来）。
        """
        return {
            "id": self.id,
            "name": self.name,
            "class": self.card_class,
            "prior": round(self.prior, 4),
            "prior_base": round(self.base_prior, 4),
            "cards": _cards_to_json(self.cards, cards),
            "tags": list(self.tags),
            "source": self.source,
            "note": self.note,
            "unexpected": self.unexpected,
        }


def _resolve_cards(raw: dict[str, int], cards: CardDB) -> dict[str, int]:
    """把「中文名或 id -> 张数」统一成 id -> 张数，丢弃数据库里找不到的。"""
    out: dict[str, int] = {}
    for key, count in raw.items():
        card = cards.by_name_or_id(key)
        if card is None:
            continue
        out[card.id] = out.get(card.id, 0) + int(count)
    return out


def _cards_to_json(cards: dict[str, int], db: CardDB | None) -> list[dict[str, Any]]:
    """`{id: 张数}` -> 带名称和费用的数组，按费用升序。

    光有 id 和张数没法人工核对；费用顺序又正好是这套牌的展开顺序，
    扫一眼就知道它是几费曲线。没有 CardDB 时退化成只有 id 和 count。
    """
    rows = []
    for cid, count in cards.items():
        card = db.get(cid) if db else None
        rows.append({
            "id": cid,
            "name": getattr(card, "name", "") or "",
            "cost": getattr(card, "cost", None),
            "count": int(count),
        })
    # 费用升序，同费用按名称再按 id —— 保证同一份数据每次写出来逐字一样
    rows.sort(key=lambda r: (r["cost"] if isinstance(r["cost"], int) else 99,
                             r["name"], r["id"]))
    return rows


def _cards_from_json(raw: Any) -> dict[str, int] | None:
    """读两种写法：新的是 `[{id,name,cost,count}]`，老的是 `{id: 张数}`。

    张数不是数值就返回 None，让整份坏文件被当成解析失败保留，别让 int() 抛穿到 load。
    """
    if not isinstance(raw, (dict, list)):
        return None
    if isinstance(raw, dict):
        if not all(isinstance(v, (int, float)) for v in raw.values()):
            return None
        return {str(k): int(v) for k, v in raw.items()}
    out: dict[str, int] = {}
    for row in raw:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        count = row.get("count") or 1
        if not isinstance(count, (int, float)):
            return None
        cid = str(row["id"])
        out[cid] = out.get(cid, 0) + int(count)
    return out


def _archetype_from_dict(d: dict[str, Any]) -> Archetype | None:
    if not isinstance(d, dict) or not d.get("id"):
        return None
    prior = d.get("prior", 1.0)
    unexpected = d.get("unexpected", 1e-4)
    base = d.get("prior_base")
    # 手改把数值写成中文/数组时别让 float() 抛穿，整份当解析失败留着给人看
    if not all(isinstance(v, (int, float)) for v in (prior, unexpected)):
        return None
    if base is not None and not isinstance(base, (int, float)):
        return None
    cards = _cards_from_json(d.get("cards") or [])
    if cards is None:
        return None
    return Archetype(
        id=str(d["id"]),
        name=d.get("name") or str(d["id"]),
        card_class=d.get("class") or "NEUTRAL",
        prior=float(prior),
        cards=cards,
        tags=list(d.get("tags") or []),
        source=d.get("source") or "file",
        note=d.get("note") or "",
        unexpected=float(unexpected),
        # 老文件没有这个字段 —— 留 None，校准时会退化用当前 prior 当起点
        prior_base=float(base) if base is not None else None,
    )


def _file_stem(archetype_id: str) -> str:
    """id 直接当文件名，只把路径分隔符之类换成下划线。"""
    return re.sub(r"[^0-9A-Za-z_.-]", "_", archetype_id) or "archetype"
