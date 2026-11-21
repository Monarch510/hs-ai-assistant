"""自对弈优化：基准和候选必须在同一批局面上配对比较。

以前 optimize() 里基准用 seed、候选却用 seed+r+1。胜率对种子极敏感，
拿不同种子的两个数字比大小会把噪声当成改进（换个种子就没了）。
这里锁住「同种子配对、换种子只留给最终 holdout」的口径。
"""

from __future__ import annotations

import unittest
from unittest import mock

from hsai import learn_optimize


class OptimizeSeedTest(unittest.TestCase):
    def _run(self, seed: int) -> list[int]:
        seeds: list[int] = []

        def fake_mirror(cards, meta, candidate, baseline, *, seed=0, **kw):
            seeds.append(seed)
            # 每次调用略好一点，逼 optimize 跑满 rounds，暴露更多候选种子
            return 0.5 + min(len(seeds), 5) * 0.01, 100

        with mock.patch.object(learn_optimize, "load_weights", return_value={"tempo": 1.0}), \
             mock.patch.object(learn_optimize, "mirror_winrate", side_effect=fake_mirror):
            learn_optimize.optimize(object(), object(), keys=["tempo"],
                                    rounds=2, step=0.4, seed=seed)
        return seeds

    def test_baseline_and_candidates_share_one_seed(self) -> None:
        for seed in (0, 7):
            with self.subTest(seed=seed):
                seeds = self._run(seed)
                self.assertTrue(seeds)
                self.assertEqual(set(seeds), {seed},
                                 f"基准与候选必须用同一个种子配对比较，实际: {seeds}")


if __name__ == "__main__":
    unittest.main()
