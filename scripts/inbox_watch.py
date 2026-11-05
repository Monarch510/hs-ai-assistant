"""收件箱哨兵：秒级轮询 docs/plan/inbox/，发现「写完的」新任务文件就报一行事件。

边界（很重要）：
- 只做「发现」：不读任务内容去执行、不移动/删除任何文件、不联网。
- 执行、验收、归档都由对话里的 agent 消费 NEWTASK 事件后完成。
- 进程随当前会话存活；关掉对话/应用即停止，不是 7x24 常驻服务。

事件格式：NEWTASK {"ts":..., "path":..., "name":..., "size":..., "sha256":...}
sha256 是内容前 12 位，供消费方确认「读到的就是检测到的那份」，避免读到半截文件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INBOX = ROOT / "docs" / "plan" / "inbox"
SUFFIXES = {".md", ".txt"}


def _scan() -> dict[str, tuple[int, int]]:
    """收当前快照：只收普通文件、纯文本、非隐藏/非下划线开头。"""
    out: dict[str, tuple[int, int]] = {}
    for p in sorted(INBOX.glob("*")):
        if not p.is_file() or p.suffix.lower() not in SUFFIXES:
            continue
        if p.name.startswith((".", "_")):
            continue
        st = p.stat()
        out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="收件箱哨兵（只发现，不执行）")
    ap.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数")
    args = ap.parse_args()

    INBOX.mkdir(parents=True, exist_ok=True)
    reported: set[str] = set()
    last: dict[str, tuple[int, int]] = {}
    print(f"WATCHING {INBOX} interval={args.interval}s", flush=True)

    while True:
        try:
            now = _scan()
            for path, sig in now.items():
                if path in reported:
                    continue
                # 连续两次采样 size+mtime 一致才算写完，避免读到写一半的文件
                if last.get(path) != sig:
                    last[path] = sig
                    continue
                p = Path(path)
                digest = hashlib.sha256(p.read_bytes()).hexdigest()[:12]
                event = {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "path": path, "name": p.name, "size": sig[0], "sha256": digest,
                }
                print("NEWTASK " + json.dumps(event, ensure_ascii=False), flush=True)
                reported.add(path)
            # 文件被移走（已归档）后清掉标记，同名新任务可以再报一次
            reported.intersection_update(now.keys())
            last = {k: v for k, v in last.items() if k in now}
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("STOPPED", flush=True)
            return 0
        except Exception as exc:
            print(f"WATCHER_ERROR {type(exc).__name__}: {exc}", flush=True)
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
