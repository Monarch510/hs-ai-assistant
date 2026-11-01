"""可选的 AI 解释层。

**它不参与决策**。出牌建议始终来自本地的规则引擎（policy.py），
这里只负责把「为什么这么打」讲成人话，补充规则引擎表达不出来的东西。

三项设计约束：

1. 密钥不落进仓库。优先读环境变量 ``HSAI_API_KEY``，
   其次读 ``data/ai.json``（该文件已在 .gitignore 里）。
2. 只在回合变化时调用一次，不跟着 320ms 的刷新循环跑。
3. 任何失败都静默降级 —— 没配 key、网络断了、模型报错，
   面板照常显示本地建议，只是少一段解释。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from . import memory
from .cards import DATA_DIR

CONFIG_PATH = DATA_DIR / "ai.json"
LESSONS_PATH = DATA_DIR / "lessons.jsonl"
TIMEOUT = 12.0

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
}

SYSTEM_PROMPT = (
    "你是炉石传说的对局解说。用户会给你当前局面的结构化数据，"
    "你只需要用 2-3 句中文解释「为什么这样打最好」或「风险在哪里」。"
    "不要给出与给定建议相反的操作，不要编造你没看到的牌。"
    "如果信息不足，就直说信息不足。"
)


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if v not in (None, "")})
    except (OSError, ValueError):
        pass
    cfg["api_key"] = os.environ.get("HSAI_API_KEY") or cfg.get("api_key") or ""
    return cfg


def is_ready() -> bool:
    cfg = load_config()
    return bool(cfg.get("enabled") and cfg.get("api_key"))


def build_prompt(snap: dict[str, Any]) -> str:
    """把局面压成一段完整但紧凑的中文描述。

    信息完整度直接决定决策质量：手牌、场面数值、法力、牌库、对手手牌数、
    最近出牌都要给 —— 只给牌名，模型没法判断能不能换、换得值不值。
    """
    our = snap.get("our") or {}
    opp = snap.get("opp") or {}
    pred = snap.get("prediction") or {}
    advice = snap.get("advice") or {}

    def v(x: Any) -> str:
        return "?" if x is None else str(x)

    def unit(c: dict[str, Any]) -> str:
        parts = [str(c.get("name") or "?")]
        if c.get("cost") is not None:
            parts.append(f"{c['cost']}费")
        if c.get("atk") is not None and (c.get("atk") or c.get("health")):
            parts.append(f"{c['atk']}/{c.get('health')}")
        elif c.get("type_cn") == "法术":
            parts.append("法术")
        tags = []
        if c.get("taunt"):
            tags.append("嘲讽")
        if c.get("divine_shield"):
            tags.append("圣盾")
        kw = re.sub(r"@（[^）]*）", "", str(c.get("keyword") or "")).strip()
        if kw:
            tags.append(kw)
        if c.get("can_attack"):
            tags.append("可攻")
        head = " ".join(parts) + (f"（{'、'.join(tags)}）" if tags else "")
        eff = str(c.get("effect") or "").strip()
        return head + (f" — {eff[:24]}" if eff else "")

    def units(items: list[dict[str, Any]]) -> str:
        return "、".join(unit(c) for c in (items or [])[:7]) or "空"

    lines = [
        f"第 {snap.get('turn')} 回合，现在是{'我方' if snap.get('is_our_turn') else '对方'}回合。",
        f"我方 {our.get('class_cn')}：血量 {v(our.get('health'))}，护甲 {our.get('armor') or 0}，"
        f"法力 {v(our.get('mana'))}/{v(our.get('max_mana'))}，牌库剩 {v(our.get('deck_count'))} 张。",
        f"我方手牌：{units(our.get('hand'))}。",
        f"我方场面：{units(our.get('board'))}。",
        f"对手 {opp.get('class_cn')}：血量 {v(opp.get('health'))}，护甲 {opp.get('armor') or 0}，"
        f"手牌 {v(opp.get('hand_count'))} 张，牌库剩 {v(opp.get('deck_count'))} 张。",
        f"对手场面：{units(opp.get('board'))}。",
    ]
    feed = snap.get("feed") or []
    if feed:
        recent = "、".join(
            f"{'我' if f.get('who') == 'our' else '敌'}打{f.get('name')}" for f in feed[-4:]
        )
        lines.append(f"最近出牌：{recent}。")
    if pred.get("name"):
        lines.append(
            f"预判对手卡组：{pred.get('name')}（{float(pred.get('confidence') or 0):.0%}），"
            f"清场风险 {float(pred.get('aoe_risk') or 0):.0%}、单解风险 {float(pred.get('removal_risk') or 0):.0%}。"
        )
    likely = pred.get("likely_hand") or []
    if likely:
        lines.append("对手可能打出的牌：" + "、".join(str(c.get("name")) for c in likely[:2]) + "。")
    # 武器：直接影响斩杀线
    for label, side in (("我方", our), ("对手", opp)):
        w = side.get("weapon") or {}
        if w.get("name"):
            lines.append(f"{label}武器：{w['name']} {w.get('atk')}/{(w.get('health') or 0)}。")
    # 对手已经打出的牌（判断他还有什么的主要依据）
    played = snap.get("opp_played") or []
    if played:
        lines.append("对手已打出：" + "、".join(str(x) for x in played[:10]) + "。")
    # 我方牌库剩余的关键牌（没有就直接跳过）
    deck = snap.get("deck") or {}
    key_cards = deck.get("key_cards") or []
    if key_cards:
        names = "、".join(f"{k.get('name')}×{k.get('count', 1)}" for k in key_cards[:4])
        lines.append(f"我方牌库剩 {deck.get('left', '?')} 张，其中关键牌：{names}。")
    # 英雄技能是否还能用
    hp = our.get("hero_power") or {}
    if hp.get("name"):
        used = "已用" if our.get("hero_power_used") else "可用"
        if "被动" in str(hp.get("text") or "")[:20]:
            used = "被动技能，无需主动使用"
        cost = f"{hp.get('cost')}费" if hp.get("cost") is not None else ""
        effect = re.sub(r"<[^>]+>", "", str(hp.get("text") or "")).replace("$", "")
        effect = re.sub(r"@（[^）]*）", "", effect).strip()
        lines.append(f"我方英雄技能：{hp['name']} {cost}（{used}）— {effect[:40]}")
    quest = snap.get("quest") or {}
    if quest.get("name"):
        lines.append(
            f"我方任务：{quest['name']}（进度 {quest.get('progress', '?')}/{quest.get('total', '?')}）"
        )
    lines.append(
        "机器判定：" + ("这回合可以斩杀。" if advice.get("lethal") else "这回合不能斩杀。")
    )

    return "\n".join(lines)


DECISION_PROMPT = (
    "你在给炉石传说对局做决策。用户会给出局面、合法动作清单（编号），"
    "以及你过去几局总结的教训。请只从清单里选一个编号。"
    "只输出 JSON，形如 {\"pick\": 编号, \"reason\": \"不超过40字的中文理由\"}。"
    "如果清单里的动作都不理想，可以选代表「结束回合」的那一项。"
    "不要编造清单里没有的动作。"
)


def recent_lessons(limit: int = 3) -> list[str]:
    """最近几条教训，用于注入 prompt。"""
    if not LESSONS_PATH.exists():
        return []
    rows: list[str] = []
    try:
        for line in LESSONS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("lesson"):
                rows.append(str(row["lesson"]))
    except OSError:
        return []
    return rows[-limit:]


def append_lessons(lessons: list[str], *, result: str = "") -> int:
    """追加教训到本地文件（不进仓库）。返回写入条数。"""
    lessons = [x.strip() for x in lessons if x and x.strip()][:3]
    if not lessons:
        return 0
    try:
        LESSONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LESSONS_PATH.open("a", encoding="utf-8") as fh:
            for text in lessons:
                fh.write(json.dumps({"ts": "", "result": result, "lesson": text},
                                    ensure_ascii=False) + "\n")
    except OSError:
        return 0
    # 只保留最近 200 条：这个文件每局都追加，不截断会越长越大，
    # 每次构建 prompt 都要全读一遍
    try:
        rows = LESSONS_PATH.read_text(encoding="utf-8").splitlines()
        if len(rows) > 200:
            LESSONS_PATH.write_text("\n".join(rows[-200:]) + "\n", encoding="utf-8")
    except OSError:
        pass
    return len(lessons)


def build_decision_prompt(snap: dict[str, Any], actions: list[str]) -> str:
    our = snap.get("our") or {}
    opp = snap.get("opp") or {}
    pred = snap.get("prediction") or {}
    lines = [build_prompt(snap), "", "合法动作（选一个编号）："]
    for i, desc in enumerate(actions):
        lines.append(f"{i}. {desc}")
    our_cls = str(our.get("class_en") or "")
    opp_cls = str(opp.get("class_en") or "")
    arch = str(pred.get("name") or "")
    lessons: list[str] = []
    for item in memory.query_lessons(our_cls, opp_cls, arch, 2):
        key = item[:6]
        if any(key in seen or seen in item[:6] for seen in [l[:6] for l in lessons]):
            continue
        lessons.append(item)
        if len(lessons) >= 2:
            break
    if lessons:
        lines.append("")
        lines.append("你之前的教训（同对局）：")
        for item in lessons:
            lines.append(f"- {item}")
    # 相似局面下你上次的选择，比抽象教训更容易照着做
    cases = memory.query_cases(our_cls, opp_cls, arch,
                               int(our.get("mana") or 0), len(our.get("board") or []), 2)
    if cases:
        lines.append("")
        lines.append("类似局面下你上次的选择：")
        for row in cases:
            outcome = "赢了" if row.get("result") == "win" else ("输了" if row.get("result") else "")
            lines.append(f"- 第{row.get('turn')}回合、{row.get('mana')}费：{row.get('action')}（{outcome}）")
    return "\n".join(lines)


def parse_decision(text: str, count: int) -> dict[str, Any] | None:
    """从模型回答里解析出动作编号。解析不出或越界就返回 None（调用方回退）。"""
    if not text or count <= 0:
        return None
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body.split("\n", 1)[-1]
    try:
        start = body.find("{")
        end = body.rfind("}")
        data = json.loads(body[start:end + 1]) if start >= 0 and end > start else {}
    except ValueError:
        return None
    pick = data.get("pick")
    if not isinstance(pick, int) or not (0 <= pick < count):
        return None
    return {"pick": pick, "reason": str(data.get("reason") or "")[:80]}


def _chat(messages: list[dict[str, str]], cfg: dict[str, Any], max_tokens: int = 220) -> str:
    body = json.dumps({"model": cfg.get("model"), "messages": messages,
                       "temperature": 0.3, "max_tokens": max_tokens}).encode("utf-8")
    url = str(cfg.get("base_url") or "").rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError):
        return ""


def decide(snap: dict[str, Any], actions: list[str],
           cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """让 AI 从合法动作里选一个。返回 {pick, reason}；不可用时 None。"""
    cfg = cfg or load_config()
    if not (cfg.get("enabled") and cfg.get("api_key")) or not actions:
        return None
    text = _chat([
        {"role": "system", "content": DECISION_PROMPT},
        {"role": "user", "content": build_decision_prompt(snap, actions)},
    ], cfg, max_tokens=120)
    return parse_decision(text, len(actions))


REVIEW_SYSTEM = (
    "你在复盘一局炉石传说。只能根据用户给出的摘要做判断，"
    "不要编造数字、不要写「第X回合有Y点斩杀」这类你没有依据的结论。"
    "每条教训必须可执行、针对具体卡组或具体打法，不超过 30 字。"
    "只输出 JSON 数组，元素是字符串。"
)


def _lesson_is_sound(text: str, missed_lethal: bool) -> bool:
    """过滤掉「编造的伤害结论」这类噪声教训。"""
    t = text.strip()
    if not t:
        return False
    # 没有漏斩的前提下，出现「X点斩杀」基本是模型脑补
    if re.search(r"\d+\s*点(斩杀|伤害)", t) and not missed_lethal:
        return False
    return True


def review(snap: dict[str, Any], summary: str,
           cfg: dict[str, Any] | None = None, *,
           missed_lethal: bool = False) -> list[str]:
    """对局结束后总结 1-3 条具体教训。失败返回空列表。"""
    cfg = cfg or load_config()
    if not (cfg.get("enabled") and cfg.get("api_key")):
        return []
    text = _chat([
        {"role": "system", "content": REVIEW_SYSTEM},
        {"role": "user", "content": summary},
    ], cfg, max_tokens=200)
    if not text:
        return []
    body = text.strip().strip("`")
    start, end = body.find("["), body.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(body[start:end + 1])
    except ValueError:
        return []
    out = [str(x)[:60] for x in items if isinstance(x, str)]
    return [x for x in out if _lesson_is_sound(x, missed_lethal)][:3]


class Conversation:
    """一局对局共用的会话。

    API 是无状态的，每次仍要重发全部消息；省的是**前缀缓存**：
    开头几轮保持不变，服务端对相同前缀按缓存价计费（约 1/10）。
    所以这里限制只保留最近几轮 —— 前缀稳定、长度可控。
    """

    def __init__(self, system: str, keep: int = 3) -> None:
        self.system = system
        self.keep = keep
        self.turns: list[tuple[str, str]] = []

    def build(self, user: str) -> list[dict[str, str]]:
        msgs = [{"role": "system", "content": self.system}]
        for u, a in self.turns[-self.keep:]:
            msgs.append({"role": "user", "content": u})
            msgs.append({"role": "assistant", "content": a})
        msgs.append({"role": "user", "content": user})
        return msgs

    def remember(self, user: str, reply: str) -> None:
        if reply:
            self.turns.append((user, reply))
            self.turns = self.turns[-self.keep:]


SESSION_SYSTEM = (
    "你在给炉石传说对局做决策。用户会给出局面、合法动作清单（编号），"
    "以及你之前几轮的判断。请从清单里选一个编号。"
    '只输出 JSON：{"pick": 编号, "reason": "不超过30字", "explain": "2-3句解释"}。'
    "不要输出 JSON 以外的内容，不要编造清单里没有的动作。"
)

KEEP_TURNS = 3


def parse_combined(text: str, count: int) -> dict[str, Any] | None:
    """解析「选择 + 理由 + 解释」三合一的结果。"""
    got = parse_decision(text, count)
    if got is None:
        return None
    body = text.strip().strip("`")
    start, end = body.find("{"), body.rfind("}")
    explain = ""
    if start >= 0 and end > start:
        try:
            data = json.loads(body[start:end + 1])
            explain = str(data.get("explain") or "")[:400]
        except ValueError:
            explain = ""
    return {"pick": got["pick"], "reason": got["reason"], "explain": explain}


def decide_and_explain(
    snap: dict[str, Any],
    actions: list[str],
    cfg: dict[str, Any] | None = None,
    conv: "Conversation | None" = None,
) -> dict[str, Any] | None:
    """一次请求同时拿到「选哪个 + 为什么」。

    合并成一次调用：延迟从 1.8 秒降到约 1 秒，成本也减半。
    """
    cfg = cfg or load_config()
    if not (cfg.get("enabled") and cfg.get("api_key")) or not actions:
        return None
    prompt = build_decision_prompt(snap, actions)
    if conv is not None:
        messages = conv.build(prompt)
    else:
        messages = [
            {"role": "system", "content": SESSION_SYSTEM},
            {"role": "user", "content": prompt},
        ]
    text = _chat(messages, cfg, max_tokens=260)
    got = parse_combined(text, len(actions))
    if got is not None and conv is not None:
        conv.remember(prompt, text)
    return got


ROUTE_SYSTEM = (
    "你在给炉石传说规划一整个回合的操作顺序。用户会给出局面和合法动作清单。"
    "请给出一个完整路线：按顺序列出这一回合你要做的所有操作。"
    "每一步必须来自清单里的动作，用清单里的原始描述。"
    "如果某一步是打出随从，请用 pos 指定站位：left（最左）、right（最右）或数字下标（0 开始）。"
    '只输出 JSON：{"steps": [{"action": "清单里的描述", "pos": "right"}], '
    '"reason": "不超过30字", "explain": "2-3句解释"}。'
    "不要输出 JSON 以外的内容。"
)


def parse_route(text: str, actions: list[str]) -> dict[str, Any] | None:
    """解析「整回合路线」，并把每一步对回合法动作。

    对不上任何合法动作的步骤不丢弃，而是标记成 unverified —— 由界面提示
    「这步需要人工判断」，而不是假装它能执行。
    """
    if not text or not actions:
        return None
    body = text.strip().strip("`")
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(body[start:end + 1])
    except ValueError:
        return None
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None

    steps: list[dict[str, Any]] = []
    for item in raw_steps[:8]:
        if isinstance(item, str):
            text_step, pos = item, ""
        elif isinstance(item, dict):
            text_step = str(item.get("action") or "")
            pos = str(item.get("pos") or "")
        else:
            continue
        text_step = text_step.strip()
        if not text_step:
            continue
        matched = next((a for a in actions if a == text_step), None)
        if matched is None:
            matched = next((a for a in actions if text_step in a or a in text_step), None)
        steps.append({
            "text": matched or text_step,
            "matched": matched is not None,
            "pos": pos,
        })
    if not steps:
        return None
    return {
        "steps": steps,
        "reason": str(data.get("reason") or "")[:80],
        "explain": str(data.get("explain") or "")[:400],
    }


def plan_route(
    snap: dict[str, Any],
    actions: list[str],
    cfg: dict[str, Any] | None = None,
    conv: "Conversation | None" = None,
) -> dict[str, Any] | None:
    """让 AI 给出一整回合的路线（含站位）。"""
    cfg = cfg or load_config()
    if not (cfg.get("enabled") and cfg.get("api_key")) or not actions:
        return None
    prompt = build_decision_prompt(snap, actions)
    if conv is not None:
        messages = conv.build(prompt)
    else:
        messages = [
            {"role": "system", "content": ROUTE_SYSTEM},
            {"role": "user", "content": prompt},
        ]
    text = _chat(messages, cfg, max_tokens=320)
    got = parse_route(text, actions)
    if got is not None and conv is not None:
        conv.remember(prompt, text)
    return got


STRATEGY_RULES = (
    "\n\n打法优先级（必须遵守）："
    "\n1. 第一优先级是斩杀对手：只要本回合的伤害够杀死对手，就按最短路径打出去，"
    "其它任何事情都不做（不解场、不铺场、不过牌）。"
    "\n2. 高攻随从优先打脸：攻击力 5 点以上的随从默认直接打英雄，"
    "不要拿它去换低攻随从；只有换了能挡住下一回合的斩杀时，才允许让它去换。"
    "\n3. 换怪交给低攻随从和法术，先把对方的威胁清掉，同时保住自己的场面。"
    "\n4. 当你判断下一回合可能被斩杀时，按以下顺序找活路："
    "\n   a. 先看自己牌库里还有没有解牌、回血或护甲；"
    "\n   b. 有抽牌手段就先抽牌，去把解牌摸上来；"
    "\n   c. 没有抽牌就找发现类效果，用发现去找解牌；"
    "\n   d. 以上都不行，才用现有手段直接解场。"
)# 追加到两个系统提示词末尾（模块导入时执行一次）
SESSION_SYSTEM = SESSION_SYSTEM + STRATEGY_RULES
ROUTE_SYSTEM = ROUTE_SYSTEM + STRATEGY_RULES


RULES_SYSTEM = (
    "你在为炉石传说解析器补「卡牌效果规则」。用户会给出引擎读不懂的卡牌及其原文，"
    "以及现有规则的格式示例。请为每张牌写一条**尽量窄**的正则规则。"
    '只输出 JSON 数组，元素形如 {"name": "牌名", "regex": "正则", "effect": {"字段": "值或$1"}, "note": "说明"}。'
    "effect 可用字段：damage, draw, heal, armor, buff_atk, buff_hp, destroy, freeze, "
    "summon_atk, summon_hp, summon_count, mana, discover, scope, aoe。"
    "数字用 $1/$2 引用正则分组。正则不要用「造成(\\d+)点伤害」这种万能写法。"
    "不要输出数组以外的内容。"
)


def parse_rules(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    body = text.strip().strip("`")
    start, end = body.find("["), body.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(body[start:end + 1])
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict) or not row.get("regex"):
            continue
        if not isinstance(row.get("effect"), dict):
            continue
        out.append({
            "name": str(row.get("name") or ""),
            "regex": str(row["regex"]),
            "effect": row["effect"],
            "note": str(row.get("note") or "")[:60],
        })
    return out[:6]


def effects_rules(unmodelled: list[dict[str, Any]],
                  cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """给未建模的牌请求规则建议。返回候选规则列表。"""
    cfg = cfg or load_config()
    if not (cfg.get("enabled") and cfg.get("api_key")) or not unmodelled:
        return []
    lines = ["需要补规则的卡牌："]
    for item in unmodelled[:4]:
        lines.append(f"- {item.get('name')}（{item.get('type') or '?'}）：{item.get('text') or ''}")
    existing = _load_existing_patterns()
    if existing:
        lines.append("")
        lines.append("现有规则格式示例：")
        for pat in existing[:3]:
            lines.append(f"  {json.dumps(pat, ensure_ascii=False)}")
    text = _chat([
        {"role": "system", "content": RULES_SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ], cfg, max_tokens=700)
    return parse_rules(text)


def _load_existing_patterns() -> list[dict[str, Any]]:
    try:
        data = json.loads((DATA_DIR / "effects.json").read_text(encoding="utf-8"))
        return list(data.get("patterns") or [])
    except (OSError, ValueError):
        return []

CRITIQUE_SYSTEM = (
    "你在帮一个炉石 AI 改进它的评估函数。用户会给出一局的摘要："
    "双方职业、对局结果、机器复盘（例如有没有漏斩）、它当时的出牌倾向。"
    "请指出这套评估函数最可能的一个偏差，并给出小幅的权重调整建议。"
    "可用权重：tempo（场面价值）、face（打脸进度）、hand（手牌差）、"
    "health（血量差）、taunt（被墙挡住）、aoe_risk（对面清场风险）。"
    '只输出 JSON：{"weights": {"名称": 数值}, "reason": "不超过60字"}。'
    "数值范围 0.5-2.0，最多调一项，不要大改。信息不足就输出 {}。"
)

from .model import DEFAULT_WEIGHTS as _DW

WEIGHT_KEYS = tuple(_DW.keys())


def parse_critique(text: str) -> dict[str, Any] | None:
    """解析 AI 对评估函数的调整建议；越界或未知权重一律丢弃。"""
    body = (text or "").strip().strip("`")
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(body[start:end + 1])
    except ValueError:
        return None
    weights = data.get("weights") if isinstance(data.get("weights"), dict) else {}
    clean: dict[str, float] = {}
    for key, val in weights.items():
        if key not in WEIGHT_KEYS:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if 0.5 <= num <= 2.0:
            clean[key] = round(num, 2)
    if not clean:
        return None
    note = data.get("reason") or data.get("note") or ""
    return {"weights": clean, "reason": str(note)[:120]}


def critique(summary: str, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """让 AI 对引擎的评估函数提一条建议（只建议，不自动生效）。"""
    cfg = cfg or load_config()
    if not (cfg.get("enabled") and cfg.get("api_key")):
        return None
    text = _chat(
        [
            {"role": "system", "content": CRITIQUE_SYSTEM},
            {"role": "user", "content": summary},
        ],
        cfg,
        max_tokens=200,
    )
    return parse_critique(text)


HINT_SYSTEM = (
    "用户在打炉石时给出自己的判断，你要把它翻译成对评估函数权重的调整。"
    "可用权重：tempo（场面价值）、face（打脸进度）、hand（手牌差）、"
    "health（血量差）、taunt（被墙挡住）、aoe_risk（对面清场风险）。"
    "只调需要调的项，倍数范围 0.5-2.0；不做调整就输出空 weights。"
    '只输出 JSON：{"weights": {"名称": 倍数}, "note": "不超过40字，说明你怎么改的"}。'
)


def analyze_hint(text: str, snap: dict[str, Any] | None = None,
                 cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """把用户的一句话判断，转成引擎权重的调整建议。"""
    cfg = cfg or load_config()
    text = (text or "").strip()
    if not text or not (cfg.get("enabled") and cfg.get("api_key")):
        return None
    context = build_prompt(snap or {})[:600]
    got = _chat(
        [
            {"role": "system", "content": HINT_SYSTEM},
            {"role": "user", "content": f"当前局面：\n{context}\n\n我的判断：{text}"},
        ],
        cfg,
        max_tokens=160,
    )
    parsed = parse_critique(got)
    if parsed is None:
        return {"weights": {}, "note": "没能转成可执行的调整"}
    return parsed
