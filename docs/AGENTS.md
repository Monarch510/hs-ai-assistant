# 项目级 AGENTS.md

给在这个仓库里干活的 AI 代理看的项目约定。**通用工作规则（三条死规则、通用约束）在根 [../AGENTS.md](../AGENTS.md)；这里只放本项目的信息，以及它依赖的文件。**

## 先读这些（按需，不必全读）

- 项目背景 / 数据流 / 完整跑法：[../README.md](../README.md)
- 项目硬规则（本仓库特有的约束）：[CONVENTIONS.md](CONVENTIONS.md)
- 踩过的坑：[PITFALLS.md](PITFALLS.md)。改代码前按关键字查（`grep` / `Select-String`），别整篇读。

## 这个项目是什么

炉石传说的本地副驾：读客户端写的 `Power.log`，用一个无边框置顶的 Tkinter 悬浮窗
告诉玩家「对面可能是什么卡组、这回合怎么打、上一局错在哪」。

**引擎做决策，AI 只在局末复盘**。未建模的牌**只记录**（写 `data/hsai.log`），
不自动调 AI 补规则、不在运行时改 `data/effects.json` —— 解析规则一改就查不到是哪局
哪条改的，而且会静默影响斩杀估算。要补规则由人看过复盘后手动加。
一局大约 1 次 AI 调用（局末），逐回合的 AI 建议默认关闭。

## 跑起来

```powershell
python -m hsai ui              # 悬浮窗（双击 启动AI助手.bat 等价）
python -m hsai ui --demo       # 示例对局，不接游戏
python -m unittest discover -s tests
python -m hsai replay <日志>    # 离线解析，输出快照 JSON
```

测试全绿是**改完的验收条件**（根 AGENTS.md 死规则 3：测试验收）。

## 代码地图

```
hsai/powerlog.py   Power.log → 事件（符号 tag、实体引用、双来源去重）
hsai/state.py      事件 → 对局状态（回合、手牌、场面、牌库、墓地、休眠、克苏恩）
hsai/model.py      局面模型 + 卡牌效果解释（parse_effect / apply_effect / evaluate）
hsai/policy.py     动作枚举、评分、斩杀检测、抢血-解场判断、发现/抉择
hsai/advisor.py    组装快照（窗口唯一数据源）
hsai/ui.py         悬浮窗（tkinter）
hsai/ai.py         AI 客户端（局末复盘 / 参数建议）
hsai/effects_ai.py AI 规则的验证器（五道闸门：目标命中；已有结果不许变（防误伤）；命中/新覆盖设上限（防正则太宽）；费用不写 mana（防白送法力）；字段须是 Effect 真有的（防静默挂上没人结算的字段））

# 上面 7 个原文件现在是「门面」：函数按职责拆进同名前缀的子模块，
# 门面把符号原样 re-export，所以 import 面不变、测试不改。
# 改哪一块就去改对应子模块，别在门面里加实现。
hsai/model_effects.py   Effect / parse_effect / warden_buff / 英雄技能
hsai/model_units.py     Unit / SideState / Board（含 apply_effect / 亡语）
hsai/model_eval.py      evaluate / resource_score / DEFAULT_WEIGHTS
hsai/policy_consts.py   常量（EPS / PRIORITY_BONUS / CTHUN_* …）
hsai/policy_threat.py   被动技能与威胁估算（最底层）
hsai/policy_actions.py  legal_actions / apply_action / best_action / 克苏恩技能
hsai/policy_lethal.py   斩杀检测
hsai/policy_race.py     抢血 / 解场判断
hsai/policy_choice.py   发现 / 抉择
hsai/policy_plan.py     plan_turn / advice_lines / validate_sequence
hsai/state_types.py     Entity / Side / ZONE_* / T_* / 效果摘要缓存
hsai/advisor_config.py  读 json / 比例值兜底
hsai/advisor_board_helpers.py  日志视图 → Unit 的纯翻译（board_from_state 本体留在 advisor.py）
hsai/ui_theme.py        配色与布局常量
hsai/ui_text.py         hud_row / _weight_summary
hsai/ui_state.py        window.json 读写
hsai/ui_ai.py           AI 线程组（Mixin）
hsai/ui_review.py       上一局复盘展示（Mixin）
hsai/ui_widgets.py      区块 / 分隔线 / 折叠 / 几何（Mixin）
hsai/ui_render.py       主渲染与分区渲染（Mixin）
hsai/cli_boot.py        _db / 控制台编码 / pythonw 流兜底
hsai/cli_*.py           各命令族（cards / meta / deck / live / ui / sim / learn /
                        setup_log / review / stats / mulligan / tags）
hsai/learn_weights.py   权重读写
hsai/learn_calibrate.py 先验校准 / 真值回填
hsai/learn_optimize.py  自对弈优化
hsai/learn_absorb.py    增量学习 / 重建 / 统计
hsai/meta_types.py      Archetype 与序列化
hsai/meta_naming.py     关键牌挑选 / 命名 / 相似度
hsai/meta_cluster.py    聚类 / 导入
hsai/infer_types.py     Candidate / Prediction / RetroTruth

# 拆模块时有三条不能破（破了不报错，只是行为悄悄不对）：
# 1. 可写模块全局必须留在门面：model._EFFECT_TABLE / effect_table() / _DB /
#    set_card_db()、state._EFFECT_CACHE、advisor._PASSIVE_RULE，以及 ui 上的
#    STATE_PATH / LAST_REVIEW_JSON / ADVICE_FEEDBACK_JSONL（tests/_ui_support.py 赋值）
#    与 ADVICE_LOG_JSONL（测试 mock.patch 改道）。
#    子模块读它们要在函数内部 `from . import <门面>` 回查，不能 import 时拷值。
# 2. 测试有源码切片/文本断言：test_advisor_board.py 用 src.index("def board_from_state")
#    切片，所以 board_from_state 必须物理留在 advisor.py，且它后面还得有个 `\ndef `；
#    test_state_reset.py 数 state.py 里 `elif kind == "block_start"` 的次数。
# 3. 依赖方向单向：model → {state, policy} → advisor；policy 内部
#    consts/threat → actions → lethal/race → choice → plan。别反向 import 门面。

# 横切各层的纯工具
hsai/tag_rules.py  12 类卡牌功能标签规则
hsai/tags.py       标签构建 / 查询 / 报告
hsai/paths.py      data/ 下所有路径的唯一出处
hsai/logutil.py    诊断日志写入（data/hsai.log，超 1 MB 自动截断）
```

## AI 复盘产出的两类东西

局末 `ai.review()` 返回 `(lessons, tunings)`，两者分开走：

- **lessons**（自然语言教训）→ `data/lessons.jsonl`，只回流给下一次 AI 的 prompt，
  **不影响引擎决策**。
- **tunings**（参数增量）→ `data/advice_tuning.jsonl`，由 `python -m hsai tunings`
  汇总给人看。默认**只读**；只有人显式加 `--apply` 才写权重。

tunings 的 `param` 只能是 `ai._TUNABLE`（= `DEFAULT_WEIGHTS` 的键 + `TAG_ORDER`
去掉「未知」）：模型编一个不存在的参数名或给 5.0 这种量级，会在
`ai._tuning_row()` 被丢掉 / 夹到 ±0.3。加新旋钮时三处一起改。

为什么不自动学：AI 每局只看一局，样本是 1；拿它直接改全局权重等于用单局噪声
训练（`review.py` 开头就是这么写的）。即便走 `--apply` 也要过三道闸：
`summarize_tunings(min_count=3)` 要求同一参数至少被 3 局独立提出、方向一致性
≥0.75（摇摆的不采纳）、单步只挪 ±0.15（不会一次跳变），且**从当前权重出发**
不冲掉自对弈结果、只改目标键、先备份 `*.json.bak`。
**真要大幅改权重，走 `hsai learn` 的自对弈，不靠 AI 一句话。**

坑：`advice_tuning.jsonl` 有写入方（`memory.add_tunings`）也必须有读取方
（`cmd_tunings`），别只写不读。

## 硬规则

1. **只用标准库**。不引入第三方依赖，`urllib` 就是 HTTP 客户端。
2. **只读日志，不注入输入**。不改客户端、不发按键、不读内存。
3. **API Key 优先从环境变量 `HSAI_API_KEY` 读；也接受 `data/ai.json` 里的 `api_key`**（该文件整目录被 `.gitignore` 忽略，绝不入库）。程序不会把 key 写进文件。
4. **`data/` 整目录忽略，只放行 `effects.json`**（`.gitignore` 里 `data/*` + `!data/effects.json`）。
   其余全是运行产物（卡牌缓存、对局记录、教训、权重、建议日志、可能含密钥的 `ai.json`），
   新增文件不用改 `.gitignore` 就自动不入库；提交前看 `git status` 里没有 `data/` 下的产物。
5. 中文注释与界面文案，与现有风格一致（短句、说「为什么」不说「做了什么」）。
6. 不可控的东西降级处理：读不到就显示「未知」，不要猜一个数字。

本仓库额外的硬约束（尚未并入上面 6 条）见 [CONVENTIONS.md](CONVENTIONS.md)。

## 坑（都是真踩过的）

完整清单见 [PITFALLS.md](PITFALLS.md)。改代码前按关键字查（`grep` / `Select-String`），
别整篇读——坑都是真的，但只有相关的那几条才影响当前改动。

## 改卡牌效果时

`parse_effect()` 认不出来时返回 `known=False`，这类牌会被建议引擎降权。
优先在 `parse_effect` 里加**文本规则**（如「消灭所有…随从」「与…等同于本牌费用」），
而不是指望 AI 补正则——AI 写的正则经常太宽或命不中目标牌。

新增字段要同时改三处：`Effect` 数据类、`parse_effect` 的赋值、`apply_effect` 的结算，
以及 `known` 的判断条件（漏了就会一直显示「未建模」）。

## 提交

```powershell
git add -A
git commit -m "一句话说清改了什么、为什么"
git push
```

提交前确认 `git status` 里没有 `data/*.jsonl`、`*.bak`、`ai.json`。
