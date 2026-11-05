# 任务包索引：炉石 AI 助手后续 7 份互不干扰的任务书

> 本目录是「盘点 → 拆任务」的产物，供分发给其他 AI 逐个执行。
> 盘点基线：`docs/plan/00-现状.md`（2026-11-05，HEAD `3177f60` + 工作区改动，`Ran 160 tests … OK`）。
> 本文件不是任务书，是索引；任务书是 `01`–`07` 共 7 份。

## 1. 文件清单与优先级

优先级按「修正确性 > 补测试 > 学习链路 > 界面体验 > 文档」。编号即建议执行顺序；同优先级可并行（文件集合互斥）。

**规模口径（M6 裁决）**：一个任务包的合理规模是「**要改的源码文件 ≤ 3 个**，新增测试文件不计入」。按这个口径，02（2 个源码 + 3 个新测试）、04（2 个源码 + 1 改 + 1 新）、06（2 个源码 + 3 个测试文件）都不算超标；**不要**按「总文件数 ≤ 3」理解，那会把本该一起改的源码和测试拆散。

| 编号 | 任务书 | 优先级 | 涉及文件 | 一句话目标 |
|---|---|---|---|---|
| 01 | `01-状态层收口.md` | P0 正确性 | `hsai/state.py`、`tests/test_state_reset.py`(新) | 局间隔离、回合口径、双方牌库分开算、清僵尸字段 |
| 02 | `02-建议链斩杀与打分.md` | P0 正确性 | `hsai/policy.py`、`hsai/advisor.py`、`tests/test_lethal_complete.py`(新)、`tests/test_choice_score.py`(新)、`tests/test_advisor_board.py`(新) | 斩杀算全（法伤/英雄攻击/风怒）、武器只建一次、抉择打分第 6 层不再恒假 |
| 03 | `03-模型层效果测试.md` | P1 补测试 | `hsai/model.py`、`tests/test_effects_model.py`(新) | Effect 三处一致性的表驱动测试 + 清掉恒为 1 的僵尸权重 |
| 04 | `04-学习链路可观测.md` | P2 学习链路 | `hsai/learn.py`、`hsai/cli.py`、`tests/test_learn.py`(改 1 行)、`tests/test_learn_report.py`(新) | `meta calibrate` 说清样本数/剔除原因/前后先验 |
| 05 | `05-聚类平均连接.md` | P2 学习链路 | `hsai/meta.py`、`tests/test_clustering_average.py`(新) | 单链聚类改平均连接，止住 A~B~C 串味 |
| 06 | `06-反馈回看与分差.md` | P3 界面体验 | `hsai/ui.py`、`hsai/feedback.py`、`tests/test_render_smoke.py`、`tests/_ui_support.py`、`tests/test_feedback_store.py`(新) | 👎 反馈重启后还能看到；抉择时显示与第二名分差 |
| 07 | `07-文档同步.md` | P4 文档 | `README.md`、`AGENTS.md` | 用例数纠正、9 条新坑合并落笔 |

## 2. 文件互斥矩阵（硬约束的机器核对依据）

每一行是一个文件，只属于一份任务书；**任意两份任务书的「涉及文件」集合没有交集**。

| 文件 | 归属任务 |
|---|---|
| `hsai/state.py` | 01 |
| `hsai/policy.py` | 02 |
| `hsai/advisor.py` | 02 |
| `hsai/model.py` | 03 |
| `hsai/learn.py` | 04 |
| `hsai/cli.py` | 04 |
| `hsai/meta.py` | 05 |
| `hsai/ui.py` | 06 |
| `hsai/feedback.py` | 06 |
| `tests/test_state_reset.py`（新） | 01 |
| `tests/test_lethal_complete.py`（新） | 02 |
| `tests/test_choice_score.py`（新） | 02 |
| `tests/test_advisor_board.py`（新） | 02 |
| `tests/test_effects_model.py`（新） | 03 |
| `tests/test_learn.py`（改 1 行断言） | 04 |
| `tests/test_learn_report.py`（新） | 04 |
| `tests/test_clustering_average.py`（新） | 05 |
| `tests/test_render_smoke.py`（追加用例） | 06 |
| `tests/_ui_support.py`（加 1 行路径改道） | 06 |
| `tests/test_feedback_store.py`（新） | 06 |
| `README.md` | 07 |
| `AGENTS.md` | 07 |

不在上表的文件（`hsai/powerlog.py`、`cards.py`、`deckcode.py`、`decks.py`、`infer.py`、`ai.py`、`memory.py`、`review.py`、`live.py`、`mulligan.py`、`effects_ai.py`、`classify.py`、`paths.py`、`logutil.py`、`sim/*`、`00-现状.md`、`docs/plan/README.md`）本批**任何任务都不改**。`data/` 与 `tests/fixtures/` 全程只读。

## 3. 分发与执行建议

- **一份任务书 = 一个 AI**。把整份 markdown 粘给它，并附一句：`遵守仓库 AGENTS.md；只改本任务书「涉及文件」里列的文件；不要动别的任务文件；不要改 data/。`
- **基线是当前工作区，不是 HEAD**：`00-现状` 盘点时 `git status` 已有 14 个未提交改动（`advisor.py`、`cli.py`、`learn.py`、`meta.py`、`paths.py`、`README.md`、`AGENTS.md` 等），**其中 `data/effects.json` 也已在基线被修改，属于正常状态**。执行者**不要** `git stash` / `checkout -- .` / 回滚别人的未提交改动；行号也以工作区当前内容为准。
- **验收要用「开工前快照」而不是「干净工作区」**：开工前先跑一次 `git status --short > /tmp/before.txt`（Windows：`git status --short > %TEMP%\before.txt`），收工再跑一次对比——**只允许新增本任务书声明的文件**，其余基线改动一律忽略（包括 `data/effects.json`、`tests/test_learn.py` 的既有 diff）。
- 可以并行：7 份任务书的文件集合互斥，同一个仓库里各干各的不会互相覆盖。01 与 02 并行时注意各自跑全量测试，不要互相改测试。
- 建议顺序：先 01/02（正确性），再 03（测试兜底），然后 04/05（学习），接着 06（界面），最后 07（文档收口）。07 最好在其它任务完成后跑，用例数才是最终值；如果先跑，它按当时实测的 `Ran N tests` 写，后续再改一次即可。
- 每份任务书都有「验收标准」一段，命令一律是 `python -m unittest …`，输出要求 `Ran N tests … OK`；**任何要求在真实 `data/` 上跑写命令的验收都写明了「不要做」**（calibrate/backfill/rebuild 一律用测试里的临时目录或 `--dry-run`）。

## 4. 跨任务的「软依赖」（不是文件冲突，执行前知道即可）

- **01 → 02**：01 让 `state` 的对手牌库数变成「30 − 对手抽牌」，02 的预判/模拟直接受益，但 02 不依赖 01 也能独立完成（各测各的）。
- **02 → 06**：02 修好 `score_choice` 第 6 层的条件；**本批不接生产**——`opp_fast` 传进 `pick_choice` 统一放到下一批单独做（现在没有可靠的「对手快攻」判断来源）。06 只负责把 `gap`（与第二名分差）显示出来，不许把 `opp_fast` 传进 `pick_choice`。**两边都不许跨文件改对方**。
- **04 ↔ 05**：04 只改校准报告，05 只改聚类判据，互不影响；05 改完跑 `meta rebuild --dry-run` 时，04 的新输出刚好能帮忙看样本。
- **06 → 07**：07 从 01–06 的第 6 节抄坑，所以 07 最后跑。

## 5. 本批明确不做（留给下一批的任务候选）

以下都来自 `00-现状.md` §7，但受「文件互斥」或「需要先拿日志样本」限制，本批不开任务：

| 候选 | 原因 | 下一批建议 |
|---|---|---|
| C3 英雄技能可用状态（`hero_power_used`） | 需要真实日志里的可靠来源；现在 `state._card_view` 根本没有 `"used"` 键 | 先抓一份用了英雄技能的对局日志，确认 tag 后再改 state/advisor/ai |
| C5 抢血/解场接入真实对手形态（`aggression_coefficient` / 固定 `opp_coeff=1.5`） | 要改 `policy.race_plan` + `advisor._advice` 的调用参数，`policy.py` 本批归 02 | 单独一份任务，先定「对手形态」从哪来（预测 tags 或已见牌） |
| C7 克苏恩按实例计数 | 横跨 `state.py` + `policy.py`（两条半截链路），两个文件本批各有任务 | 单独任务：先定「按实例」还是「按全局」的唯一口径，再清 `_cthun_by_unit`/`_log_cthun`/`cthun_by_source` |
| C2 墓地进 `Side.as_dict()` 并在窗口/AI 显示 | `state.py` 与 `ui.py`/`ai.py` 跨文件，且当前只有 advisor 内部用 | 先定展示位置（复盘还是实时面板），再一次性打通 |
| C10 👎 反馈并入 `lessons.jsonl` | 06 只做「重启可见」；并入学习链路会改 `memory`/`ui` 两处 | 与「AI 复盘教训」一起做，先定教训去重规则 |
| C8 其余死代码（`ai.recent_lessons`/`append_lessons`、`policy._log_cthun`、`aggression_coefficient` 等） | 跨 `ai.py`/`policy.py`，且部分要等 C3/C5/C7 定口径 | 口径定完再统一删/接线，避免删了又加 |
| 冲锋/突袭没有独立规则（`model` 只认 `keyword`，突袭「当回合只能打随从」的口径散在 policy） | 会影响 `max_face_damage` 口径，本批 02 不动语义 | 与 C5 一起做：先定「突袭能不能打脸」的唯一判定点 |
| 法术墓地缺失（`ZONE_GRAVE` 只记随从） | 需要先确认哪些牌要读「法术墓地」，否则是空实现 | 等 C2 墓地打通后一并做 |
| `state.py:214-215` 注释与代码错位 | 纯注释，归 07 但要等 01 改完那一段才不冲突 | 07 收口时顺手修，或在 01 完成后单独一句 |
| C12 状态模型补字段（对手奥秘、对手武器耐久、对手手牌身份） | 需要先确认日志字段与 UI 展示需求 | 先做只读盘点（下一份 `00-现状` 续篇） |
| 聚类第二弱点（两个变体各占一半 → 模板变并集） | 05 只做平均连接；变体识别要新设计 | 05 完成后观察真实数据，再决定「签名加局数下限」的具体规则 |

## 6. 验收总闸

- 每份任务完成时都必须跑：`python -m unittest discover -s tests` → `Ran N tests … OK`，**N ≥ 开工时基线数 + 本任务实际新增数**（基线数字以开工前实测为准，任务书里写的 160 只是 2026-11-05 的快照）。
- 每份任务都不许修改别人的测试文件；测试红了先怀疑实现，不要先改断言。
- **文件卫生按「开工前 `git status --short` 快照」判断**：与快照相比，只允许出现本任务书声明的文件；基线里已有的改动（含 `data/effects.json`、`tests/test_learn.py` 的既有 diff）不算你的；`*.bak`、`ai.json` 任何时候都不该新增；`docs/plan/00-现状.md` 保持原样。
- 如果某个任务执行中发现「必须改别的任务文件才能完成」：停下来，写进该任务的交接物，不要跨文件改——这是本批互不干扰约定的底线。
