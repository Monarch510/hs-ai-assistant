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

# 卡牌功能标签（11 类）。card_tags.json 可重生成；ai/manual 是补标输入，
# pending 是规则拿不准、等人/AI 补的清单。
CARD_TAGS_JSON = DATA_DIR / "card_tags.json"
CARD_TAGS_AI_JSON = DATA_DIR / "card_tags_ai.json"
CARD_TAGS_MANUAL_JSON = DATA_DIR / "card_tags_manual.json"
CARD_TAGS_PENDING_JSONL = DATA_DIR / "card_tags_pending.jsonl"


# ---------- 卡组与环境 ----------

# 卡组原型库：一个原型一个文件，`data/meta/<id>.json`。
# 单个 meta.json 要改一个原型就得重写整份文件，也没法手工增删一套牌。
META_DIR = DATA_DIR / "meta"
META_BACKUP_DIR = DATA_DIR / "meta_backup"
# 老格式（单文件）。只在迁移时读一次，之后改名留档。
META_JSON = DATA_DIR / "meta.json"
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
# AI 局末复盘给出的「可执行调整建议」，一条一行。只是建议，不自动生效：
# 同一参数要积累够次数才值得谈，避免拿单局噪声去改全局权重。
ADVICE_TUNING_JSONL = DATA_DIR / "advice_tuning.jsonl"


# ---------- 权重 ----------

POLICY_WEIGHTS_JSON = DATA_DIR / "policy_weights.json"
MULLIGAN_WEIGHTS_JSON = DATA_DIR / "mulligan_weights.json"
# 动态标签权重的局外覆盖表（缺/坏文件由调用方降级成空表）
# 格式：{"weights": {...}, "mulligan_scale": 3.0, "play_scale": 1.0}
TAG_WEIGHTS_JSON = DATA_DIR / "tag_weights.json"


# ---------- 配置与运行状态 ----------

AI_CONFIG_JSON = DATA_DIR / "ai.json"               # 可能含 key，绝不入库
WINDOW_JSON = DATA_DIR / "window.json"              # 窗口位置/透明度/字号/折叠
INSTALL_CACHE_TXT = DATA_DIR / "install_dir.txt"    # 探测到的游戏安装目录
LOG_FILE = DATA_DIR / "hsai.log"                    # 运行时诊断日志
