"""命令行：卡牌库查看与更新。"""

from __future__ import annotations

import argparse

from . import cards as cards_mod


def cmd_cards(args: argparse.Namespace) -> int:
    if args.update or not cards_mod.cache_path().exists():
        db = cards_mod.update()
    else:
        db = cards_mod.load()
    print(f"卡牌库：{len(db)} 张，缓存于 {cards_mod.cache_path()}")
    if args.search:
        for c in list(db.by_id.values()):
            if args.search in c.name or args.search.upper() == c.id:
                print(f"  {c.id:<18} {c.name:<12} {c.cost}费 {c.class_cn}{c.type_cn} {c.atk}/{c.health} {c.keyword_cn}")
    return 0
