"""`data/` 下所有运行产物的路径 —— 唯一允许出现字面量文件名的地方。

为什么集中
----------
这些路径原本散在 14 个模块里，其中三个被**重复定义**：

    lessons.jsonl    ai.py 一份、memory.py 一份
    effects.json     ai.py / effects_ai.py / model.py 各一份
    hsai.log         advisor.py / state.py / cli.py 各一份

两份定义只要有一边改了（改文件名、加子目录、换安装位置），写和读就会错位，
而且**不会报错** —— 表现成「教训记了但检索不到」这种查不出原因的问题。

约定
----
· 只定义路径，不做 IO；读写由各模块自己负责
· 全部相对 ROOT 推导，整个目录搬走不用改代码
· 除 `EFFECTS_JSON` 外都是运行产物，不进仓库（见 .gitignore）
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


# ---------- 卡牌与规则 ----------

def cards_cache(locale: str = "zhCN") -> Path:
    """卡牌库缓存，按语言分文件（首次运行下载，约 4 MB）。"""
    return DATA_DIR / f"cards_{locale}.json"


# 卡牌效果正则规则表。**这一份入库**：它是可复现的行为定义，不是产物
EFFECTS_JSON = DATA_DIR / "effects.json"
EFFECTS_BACKUP = DATA_DIR / "effects.json.bak"


# ---------- 卡组与环境 ----------

META_JSON = DATA_DIR / "meta.json"                  # 卡组原型库
META_BACKUP = DATA_DIR / "meta.backup.json"
OUR_DECK_JSON = DATA_DIR / "our_deck.json"          # deck mine 登记的自己卡组


# ---------- 对局记录 ----------

OBSERVATIONS_JSONL = DATA_DIR / "observations.jsonl"   # 每局的职业/已见牌/胜负
GAMES_JSONL = DATA_DIR / "games.jsonl"                 # 完整对局记录
LAST_REVIEW_JSON = DATA_DIR / "last_review.json"       # 上一局复盘（窗口收起时显示）
ADVICE_LOG_JSONL = DATA_DIR / "advice_log.jsonl"       # 发出去的每条建议
ADVICE_FEEDBACK_JSONL = DATA_DIR / "advice_feedback.jsonl"  # 「不赞同」回合的 AI 复盘


# ---------- 长期记忆 ----------

LESSONS_JSONL = DATA_DIR / "lessons.jsonl"          # AI 总结的教训（带对局标签）
CASES_JSONL = DATA_DIR / "cases.jsonl"              # 具体局面 → 打法 → 结果
OPP_HISTORY_JSONL = DATA_DIR / "opp_history.jsonl"  # 对手形态历史


# ---------- 权重 ----------

POLICY_WEIGHTS_JSON = DATA_DIR / "policy_weights.json"
MULLIGAN_WEIGHTS_JSON = DATA_DIR / "mulligan_weights.json"


# ---------- 配置与运行状态 ----------

AI_CONFIG_JSON = DATA_DIR / "ai.json"               # 可能含 key，绝不入库
WINDOW_JSON = DATA_DIR / "window.json"              # 窗口位置/透明度/字号/折叠
INSTALL_CACHE_TXT = DATA_DIR / "install_dir.txt"    # 探测到的游戏安装目录
LOG_FILE = DATA_DIR / "hsai.log"                    # 运行时诊断日志
