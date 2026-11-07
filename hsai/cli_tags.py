"""命令行：卡牌功能标签：构建 / 报告 / 待定清单。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .cli_boot import _db
from .paths import CARD_TAGS_AI_JSON, CARD_TAGS_MANUAL_JSON


def cmd_tags(args: argparse.Namespace) -> int:
    """卡牌功能标签：构建全量数据 / 覆盖率报告 / 导出待定清单。"""
    from . import tags

    if args.action == "build":
        db = _db()
        payload = tags.build(
            db,
            ai=tags.load_source(CARD_TAGS_AI_JSON),
            manual=tags.load_source(CARD_TAGS_MANUAL_JSON),
        )
        path = tags.save(payload)
        print(tags.format_report(payload))
        print(f"已写入 {path}")
        return 0

    payload = tags.load_payload()
    if not payload.get("rows"):
        print("还没有标签数据：先运行 `python -m hsai tags build`")
        return 2
    if args.action == "report":
        print(tags.format_report(payload))
        return 0
    if args.action == "pending":
        path = tags.save_pending(payload)
        print(f"待定 {len(payload.get('pending') or [])} 张 -> {path}")
        return 0
    return 1
