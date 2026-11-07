"""建议引擎：这一回合怎么打。

这个模块现在是**门面**，按职责拆成了几块：
  · policy_consts.py   模块级常量（EPS / PRIORITY_BONUS / CTHUN_* …）
  · policy_threat.py   被动技能与威胁估算（最底层，不依赖其它块）
  · policy_actions.py  动作枚举与模拟底座（legal_actions / apply_action / best_action）
  · policy_lethal.py   斩杀检测（max_face_damage / _lethal_sequence）
  · policy_race.py     抢血 / 解场判断（race_plan）
  · policy_choice.py   发现 / 抉择（pick_choice / score_choice）
  · policy_plan.py     路线规划（plan_turn / advice_lines / validate_sequence）

依赖方向严格单向：
  model → policy_consts/policy_threat → policy_actions → policy_lethal/policy_race
        → policy_choice → policy_plan → 本门面
所以 plan_turn 能调斩杀、抢血、抉择，反过来不行。

它是 1 层搜索（每步只看一步后果），不是 MCTS。所以它擅长：
斩杀、交换、站场、节奏；不擅长：多回合组合技、需要保留资源的运营。
这个边界会如实写在建议理由里，不假装懂更多。
"""

from __future__ import annotations

from .policy_consts import (  # noqa: F401
    CHOOSE_ONE_RE,
    CTHUN_MULTIPLIER,
    CTHUN_SKILL_NAMES,
    EPS,
    MAX_STEPS,
    PRIORITY_BONUS,
    TAG_WEIGHT_SCALE,
    _GIGANTIC_CACHE,
)

from .policy_threat import (  # noqa: F401
    battlecry_times,
    board_threat,
    passive_rule,
    unit_threat,
)

from .policy_actions import (  # noqa: F401
    Action,
    _find_unit,
    _gigantic_appendages,
    _spell_damage,
    apply_action,
    apply_cthun_skill,
    best_action,
    categorize,
    choose_one_branches,
    legal_actions,
)

from .policy_lethal import _lethal_sequence, max_face_damage  # noqa: F401

from .policy_race import (  # noqa: F401
    _hand_burst,
    _has_profitable_trade,
    _mode_allows,
    _turns_after,
    race_plan,
)

from .policy_choice import (  # noqa: F401
    _cthun_choice,
    _deck_theme,
    _spell_damage_on_board,
    pick_choice,
    score_choice,
)

from .policy_plan import (  # noqa: F401
    _reason,
    _score_from_gain,
    _tag_bonus,
    advice_lines,
    plan_turn,
    validate_sequence,
)
