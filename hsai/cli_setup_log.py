"""命令行：一键写 log.config。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def cmd_setup_log(args: argparse.Namespace) -> int:
    """一键把 log.config 写到炉石安装目录。

    为什么需要这个命令：炉石不写 Power.log 的唯一原因就是它所在目录没有 log.config。
    而不同版本/安装方式的客户端，读的是不同目录 —— 这里靠「运行中的 Hearthstone.exe」
    来定位，比硬编码 %LOCALAPPDATA% 靠谱。
    """
    from .logwatch import find_power_log, log_config_targets, setup_log_config

    extra = [Path(args.dir)] if args.dir else []
    targets = log_config_targets(extra)
    if not targets:
        print("没找到炉石安装目录（需要包含 Hearthstone.exe 或 Hearthstone_Data）。")
        print('可以手动指定：python -m hsai setup-log --dir "D:\\software\\Hearthstone"')
        return 2

    results = setup_log_config(extra, dry_run=args.dry_run)
    changed = [r for r in results if r[1].startswith(("已创建", "已覆盖"))]
    if args.auto and not changed and not args.dry_run:
        print(f"日志开关：已开启（{targets[0]}）")
        return 0

    print("日志开关：")
    for path, msg in results:
        print(f"  {path}\n    -> {msg}")
    if not args.dry_run and changed:
        print()
        print("生效方式：完全退出炉石客户端（不是最小化），重新打开，进一局对战。")
        print("之后 Power.log 会出现在安装目录的 Logs 子目录里，助手会自动接上。")
    found = find_power_log(extra)
    print(f"当前找到的日志：{found}" if found else "当前还没有 Power.log（重启游戏后会出现）。")
    return 0
