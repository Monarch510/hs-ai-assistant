"""policy 的模块级常量。单独放一层，让各策略块都能 import 而不成环。"""

from __future__ import annotations

import re


EPS = 0.35          # 提升小于这个值就不算改进

# 动态标签权重的出牌缩放（DYNAMIC-WEIGHTS §3）：12 类权重是「局面 → 每类多值多少」
# 的相对分，乘这个系数才和 gain 同一档。一张牌通常命中 1-3 类、基础权重 0.4-1.0，
# 叠加分约 1-3 分 —— 低于「推进场面」的 3.0，只改同档排序，不推翻评分主序。
TAG_WEIGHT_SCALE = 1.0

# 用户定的优先级：斩杀 > 对场面有帮助 > 过牌 > 发现 > 防斩杀。
# 用「分类加分」实现：它不会推翻明显更好的选择，但同档收益时按这个顺序偏向。
PRIORITY_BONUS = {
    "lethal": 1000.0,
    "free_trade": 6.0,
    "board": 3.0,
    "draw": 1.6,
    "discover": 1.0,
    "defend": 0.6,
    "other": 0.0,
}
MAX_STEPS = 6

CHOOSE_ONE_RE = re.compile(r"抉择[：:]\s*(.+)$", re.S)

_GIGANTIC_CACHE: dict[str, list] = {}

CTHUN_SKILL_NAMES = ("克苏恩的愤怒", "克苏恩的领域", "克苏恩的凝视", "克苏恩的仆从")

# 卡牌库 BE_013「集齐四个技能后效果变为四倍」；结算和打分都读这一处，
# 免得改了模拟里的倍数、建议理由还按旧倍数写。
# 参数名 tripled 是三倍时代留下的，接口沿用以免牵动快照/界面。
CTHUN_MULTIPLIER = 4
