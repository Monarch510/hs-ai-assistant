"""第 14 条残留「tunings --apply 不幂等」的回归护栏。

只依赖标准库；落盘全用 tempfile 改道，绝不碰真实 data/。
语义：一次 --apply 把当前日志里的局记成「已兑现」，要再挪一步必须等攒够
min_count 局新复盘，而不是把同一批证据反复叠加。
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from hsai import cli_stats, memory
from hsai.model import DEFAULT_WEIGHTS


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.tune = d / "advice_tuning.jsonl"
        self.pw = d / "policy_weights.json"
        self.tw = d / "tag_weights.json"
        self.applied = d / "advice_tuning_applied.json"

        patcher = mock.patch.object(memory, "ADVICE_TUNING_PATH", self.tune)
        patcher.start()
        self.addCleanup(patcher.stop)
        pw = mock.patch.object(cli_stats, "POLICY_WEIGHTS_JSON", self.pw)
        pw.start()
        self.addCleanup(pw.stop)
        tw = mock.patch.object(cli_stats, "TAG_WEIGHTS_JSON", self.tw)
        tw.start()
        self.addCleanup(tw.stop)

        def _load_weights() -> dict[str, float]:
            try:
                w = json.loads(self.pw.read_text(encoding="utf-8")).get("weights") or {}
            except (OSError, ValueError):
                w = {}
            return {**DEFAULT_WEIGHTS, **w}

        lw = mock.patch.object(cli_stats.learn, "load_weights", side_effect=_load_weights)
        lw.start()
        self.addCleanup(lw.stop)

    def _seed(self, n: int, delta: float = 0.1, param: str = "tempo") -> None:
        # 一局 = 一次 add_tunings；n 局独立提出才凑得够 min_count
        for _ in range(n):
            memory.add_tunings(
                [{"kind": "param", "param": param, "delta": delta, "why": "a"}], {})

    def _run(self, *, apply: bool) -> int:
        with redirect_stdout(io.StringIO()):
            return cli_stats.cmd_tunings(
                argparse.Namespace(min_count=3, apply=apply))

    def _tempo(self) -> float:
        return json.loads(self.pw.read_text(encoding="utf-8"))["weights"]["tempo"]


class WatermarkIdempotencyTest(_Fixture):
    def test_repeated_apply_without_new_data_does_not_drift(self) -> None:
        self._seed(3)
        self._run(apply=True)
        self.assertAlmostEqual(self._tempo(), 1.15)
        self._run(apply=True)       # 无新证据，不该再挪
        self._run(apply=True)
        self.assertAlmostEqual(self._tempo(), 1.15)

    def test_new_evidence_allows_exactly_one_more_step(self) -> None:
        self._seed(3)
        self._run(apply=True)
        self.assertAlmostEqual(self._tempo(), 1.15)
        self._seed(3)               # 3 局新复盘
        self._run(apply=True)
        self.assertAlmostEqual(self._tempo(), 1.30)
        self._run(apply=True)       # 又不漂移
        self.assertAlmostEqual(self._tempo(), 1.30)

    def test_read_only_does_not_advance_watermark(self) -> None:
        self._seed(3)
        self._run(apply=False)
        self.assertFalse(self.applied.exists())     # 只读不落水位
        self.assertFalse(self.pw.exists())          # 也不写权重
        self._run(apply=True)
        self.assertAlmostEqual(self._tempo(), 1.15)

    def test_fewer_than_min_count_new_games_blocks_step(self) -> None:
        self._seed(3)
        self._run(apply=True)
        self._seed(2)               # 只来 2 局新证据，不够 3
        self._run(apply=True)
        self.assertAlmostEqual(self._tempo(), 1.15)

    def test_broken_watermark_degrades_to_not_applied(self) -> None:
        self._seed(3)
        self.applied.write_text("{ 坏水位", encoding="utf-8")
        self._run(apply=True)       # 坏文件当成从没应用过，仍挪一步
        self.assertAlmostEqual(self._tempo(), 1.15)
        self._run(apply=True)       # 之后水位已修好，不再漂移
        self.assertAlmostEqual(self._tempo(), 1.15)

    def test_watermark_records_current_game_ids(self) -> None:
        self._seed(3)
        self._run(apply=True)
        data = json.loads(self.applied.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 3)              # 3 个独立局全部记下


if __name__ == "__main__":
    unittest.main()
