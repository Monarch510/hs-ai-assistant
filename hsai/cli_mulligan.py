"""命令行：离线测试留牌建议。"""

from __future__ import annotations

import argparse

from . import mulligan
from .cli_boot import _db
from .infer import DeckPredictor
from .meta import MetaDB


def cmd_mulligan(args: argparse.Namespace) -> int:
    """离线检查：给定手牌（卡牌名）和对手职业，看留牌建议。"""
    db = _db()
    meta = MetaDB.load(db)
    pred = DeckPredictor(meta, db).predict(args.opp, [], turn=1, hand_count=4)
    hand = [c for c in (db.by_name_or_id(n) for n in args.hand) if c]
    if not hand:
        print("没有可识别的卡牌名")
        return 2
    rows = mulligan.advise(hand, cards=db, prediction=pred, is_first=not args.second)
    print(mulligan.summary(rows))
    for r in rows:
        print(f"  [{r['action']:<4}] {r['name']:<10} {r['cost']}费  分数 {r['score']:>3}  {r['reason']}")
    return 0
