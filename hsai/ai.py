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
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any

from . import memory
from .paths import AI_CONFIG_JSON

CONFIG_PATH = AI_CONFIG_JSON
TIMEOUT = 12.0

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
}

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


def enabled(cfg: dict[str, Any]) -> bool:
    """开关打开、且配了 key，才值得发请求 —— 否则白等 12 秒超时。"""
    return bool(cfg.get("enabled") and cfg.get("api_key"))


def is_ready() -> bool:
    return enabled(load_config())


def json_array(text: str) -> list[Any]:
    """从模型回复里抠出 JSON 数组，失败返回空列表。

    模型常把数组包在 ``` 里，或前后加一句「以下是结果」。所以按第一个 `[`
    到最后一个 `]` 截取，而不是指望整段就是合法 JSON。
    """
    body = (text or "").strip().strip("`")
    end = body.rfind("]")
    start = body.find("[")
    while 0 <= start < end:
        try:
            return json.loads(body[start:end + 1])
        except ValueError:
            # 前言里的 [说明] 会让首个 [ 截出半截，递进到下一个 [ 再试
            start = body.find("[", start + 1)
    return []


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
    deck = snap.get("deck") or {}
    key_cards = deck.get("key_cards") or []
    if key_cards:
        names = "、".join(f"{k.get('name')}×{k.get('count', 1)}" for k in key_cards[:4])
        lines.append(f"我方牌库剩 {deck.get('left', '?')} 张，其中关键牌：{names}。")
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
    if not isinstance(pick, int) or isinstance(pick, bool) or not (0 <= pick < count):
        return None
    return {"pick": pick, "reason": str(data.get("reason") or "")[:80]}


def chat(messages: list[dict[str, str]], cfg: dict[str, Any], max_tokens: int = 220) -> str:
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


REVIEW_SYSTEM = (
    "你在复盘一局炉石传说。只能根据用户给出的摘要做判断，"
    "不要编造数字、不要写「第X回合有Y点斩杀」这类你没有依据的结论。\n"
    "只输出 JSON 数组，元素是对象，两种二选一：\n"
    '  {"lesson": "可执行的教训，不超过 30 字"}\n'
    '  {"param": "参数名", "delta": 数值, "why": "为什么，不超过 30 字"}\n'
    "param 只能取这些（这是决策引擎真实存在的旋钮）：\n"
    "  权重类 tempo/face/hand/health/taunt/aoe_risk/fatigue —— 表示「这类因素"
    "这局应该更重(正)还是更轻(负)」，delta 在 -0.3~0.3 之间。\n"
    "  标签类 单解/群解/站场/增益/减益/治疗/护甲/资源/抽牌/斩杀/保护 —— 同上，"
    "表示这类牌这局该更重视(正)还是更不重视(负)，delta 在 -0.3~0.3 之间。\n"
    "一条都没有就输出空数组 []。多数局应该只有 lesson，别硬凑 param。"
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


def _tuning_row(name: str, delta: Any, why: Any) -> dict[str, Any] | None:
    """把一条 AI 建议收成可存的行；参数名不认识或数值越界就丢掉。

    为什么要在这里卡死：这些 delta 以后会被拿去改引擎权重，
    模型编一个不存在的参数名（比如 "aggression"）或给 5.0 这种量级，
    下游会静默当成有效建议累积。
    """
    param = str(name or "").strip()
    if param not in _TUNABLE:
        return None
    try:
        amount = float(delta)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount):
        return None
    # 单局建议幅度必须很小，否则一眼就是模型在放飞
    amount = max(-0.3, min(0.3, amount))
    if abs(amount) < 0.02:
        return None
    return {"kind": "param", "param": param, "delta": round(amount, 3),
            "why": str(why or "").strip()[:60]}


# 引擎真实存在的旋钮：权重键 + 标签名。加新旋钮时这里和 DEFAULT_WEIGHTS /
# TAG_ORDER 一起改，否则 AI 提了也没人认。
def _tunable_params() -> set[str]:
    from .model import DEFAULT_WEIGHTS
    from .tag_rules import TAG_ORDER

    return set(DEFAULT_WEIGHTS) | {t for t in TAG_ORDER if t != "未知"}


_TUNABLE = _tunable_params()


def parse_review(text: str, *, missed_lethal: bool = False) -> tuple[list[str], list[dict[str, Any]]]:
    """把模型回复拆成 (教训列表, 调整建议列表)。

    两者分开存：教训是给人/AI 看的自然语言，调整建议是给引擎的参数增量。
    """
    lessons: list[str] = []
    tunings: list[dict[str, Any]] = []
    for row in json_array(text):
        if isinstance(row, str):
            # 兼容模型仍按旧格式吐裸字符串
            if _lesson_is_sound(row, missed_lethal):
                lessons.append(str(row)[:60])
            continue
        if not isinstance(row, dict):
            continue
        if row.get("param"):
            item = _tuning_row(row.get("param"), row.get("delta"), row.get("why"))
            if item is not None:
                tunings.append(item)
                continue
        text_ = str(row.get("lesson") or "")
        if _lesson_is_sound(text_, missed_lethal):
            lessons.append(text_[:60])
    return lessons[:3], tunings[:3]


def review(snap: dict[str, Any], summary: str,
           cfg: dict[str, Any] | None = None, *,
           missed_lethal: bool = False) -> tuple[list[str], list[dict[str, Any]]]:
    """对局结束后总结 1-3 条教训，外加至多 3 条引擎参数调整建议。

    返回 (lessons, tunings)；失败返回两个空列表。
    """
    cfg = cfg or load_config()
    if not enabled(cfg):
        return [], []
    text = chat([
        {"role": "system", "content": REVIEW_SYSTEM},
        {"role": "user", "content": summary},
    ], cfg, max_tokens=400)
    return parse_review(text, missed_lethal=missed_lethal)


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
    if not enabled(cfg) or not actions:
        return None
    prompt = build_decision_prompt(snap, actions)
    if conv is not None:
        messages = conv.build(prompt)
    else:
        messages = [
            {"role": "system", "content": SESSION_SYSTEM},
            {"role": "user", "content": prompt},
        ]
    text = chat(messages, cfg, max_tokens=260)
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
    if not enabled(cfg) or not actions:
        return None
    prompt = build_decision_prompt(snap, actions)
    if conv is not None:
        messages = conv.build(prompt)
    else:
        messages = [
            {"role": "system", "content": ROUTE_SYSTEM},
            {"role": "user", "content": prompt},
        ]
    text = chat(messages, cfg, max_tokens=320)
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

