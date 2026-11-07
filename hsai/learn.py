"""自主学习。

这里没有任何魔法，只有三件真事：

1. **对局记录**：每局结束把「对手职业 / 已暴露的牌 / 我们的胜负」写进
   `data/observations.jsonl`。用得越多，本地数据越厚。
   注意这里存的是**预测和真值两份**：预测只用来算准确率，学习只吃真值
   （`infer.retrospect()` 局末回看的那份）。拿预测当真值会让先验自我确认。
2. **从自己的对局里自举环境**：`build_local_meta` 把观察到的牌集合聚类成
   卡组原型（见 meta.build_from_observations），不需要任何外部网站。
3. **权重自对弈优化**：在同一套牌（镜像对局）里让「候选权重」对打「基准权重」，
   胜率高就采纳。这是坐标轮换爬山，不是强化学习，但它是真在优化的闭环。

必须说清楚的边界：第 3 步优化的是**本仓库模拟器里的胜率**，模拟器对卡牌效果
只建模了常见子集。所以它调出来的权重是「在这个近似环境里更能赢」，
不等于天梯胜率提升。它的价值在于：让评估函数的比例至少在内部一致、可复现，
而不是靠拍脑袋。
"""

from __future__ import annotations

# 方向保持不变：advisor 只在函数内延迟 `from . import learn` 回查 absorb_observations。
from .advisor import OBSERVATIONS_PATH, WEIGHTS_PATH
from .model import DEFAULT_WEIGHTS

# 门面：函数/数据类拆到 learn_* 子模块，显式 re-export 维持原有 import 面。
# cli / test_learn 用 learn.X 模块限定访问；advisor 用 learn_mod.absorb_observations。
from .learn_weights import (
    Report, load_weights, save_weights, load_mulligan_weights, save_mulligan_weights,
)
from .learn_calibrate import (
    build_local_meta, _row_truth, calibrate_priors, backfill_truth,
)
from .learn_optimize import (
    mirror_winrate, optimize, optimize_mulligan,
)
from .learn_absorb import (
    _overlap, absorb_observations, rebuild_learned, stats,
)
