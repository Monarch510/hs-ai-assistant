"""命令行：对局统计。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import learn


def cmd_stats(args: argparse.Namespace) -> int:
    data = learn.stats()
    print(json.dumps(data, ensure_ascii=False, indent=1))
    return 0
