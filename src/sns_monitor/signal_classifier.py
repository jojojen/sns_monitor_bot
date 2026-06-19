"""LLM-as-judge classifier mapping a single SNS tweet to two opportunity
signals: 📈 long-term investment and ⚡ immediate arbitrage.

Run order in the monitor:
  entity_extractor.extract_entities() → knowledge_db.retrieve summaries →
  interest_profile.build_user_interest_profile() → classify_sns_signal() →
  storage.record_sns_signal() → (bypass A check + score gate) → notify

The classifier is the prompt + JSON parser. It does NOT touch the DB itself
(caller writes the result). On any failure (LLM error / non-JSON response)
the fallback returns conservative zero-scores so the gate drops the tweet
rather than spamming.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnsPostSignal:
    tweet_id: str
    rule_id: str
    long_term_score: int            # 📈 future-value signal strength (0-100)
    arbitrage_score: int            # ⚡ immediate-action signal strength (0-100)
    matched_products: tuple[str, ...]   # products from user profile that the tweet mentions
    matched_keywords: tuple[str, ...]   # general TCG keywords ("抽選" / "再販" / "予約")
    matched_entities: tuple[str, ...]   # canonical names from knowledge base
    suggested_action: str               # one-line imperative
    rationale: str                      # ≤ 60-char "why this score"
    deadline_iso: str | None            # ISO8601 if extractable, else None
    actionability: str = "vague"        # "concrete" | "vague" — silence-first default
    purchase_target_json: str | None = None  # JSON {sku_or_title, purchase_url, price_hint} when concrete
    giveaway_spam: bool = False         # follow+retweet 無償抽獎 raffle — worthless, drop even on keyword match
    is_event_signal: bool = False       # limited IP event/collab announcement — push even at announce stage
    event_name: str = ""                # 活動名稱
    event_date: str = ""                # 時間: ISO8601 event/sale start when derivable, else free text/""
    event_location: str = ""            # 地點
    signup_url: str = ""                # 報名連結: application/ticket/前売券 URL when advance purchase needed
    recommended_character: str = ""     # 目標商品（角色）: hot character or target 限定商品 to buy; "" when uncertain


# Score thresholds — kept here so tests can override without re-deploying.
DEFAULT_MIN_SCORE_TO_PUSH: int = 60

# Positive feedback lowers the push threshold for a rule (raises push
# probability of similar future tweets) instead of changing scan frequency.
# Conservative: 👍 -2, 💰 -5 per occurrence (30-day aggregate), floored at 50
# so noise (<50) is never surfaced. Explicit-keyword bypass is unaffected.
FEEDBACK_SCORE_FLOOR: int = 50
FEEDBACK_BOOST_PER_UP: int = 2
FEEDBACK_BOOST_PER_BOUGHT: int = 5


def build_classifier_prompt(
    *,
    tweet_id: str,
    author_handle: str,
    created_at: str,
    tweet_text: str,
    watchlist_queries: Sequence[str],
    pinned_targets: Sequence[str],
    feedback_for_rule: Mapping[str, int],
    knowledge_block: str,
    heat_block: str = "",
) -> str:
    """Compose the LLM prompt. Public for testability — assert against
    rendered text in unit tests."""
    def _list_or_none(items: Sequence[str], limit: int = 30) -> str:
        if not items:
            return "（無）"
        shown = list(items)[:limit]
        suffix = "…" if len(items) > limit else ""
        return "、".join(shown) + suffix

    fb = feedback_for_rule or {}
    up = int(fb.get("up", 0))
    bought = int(fb.get("bought", 0))
    down = int(fb.get("down", 0))

    return (
        "你是 TCG 收藏品商機分類器。給一則 SNS 推文 + 使用者目前的興趣檔案，\n"
        "判斷這則推文是否帶有以下兩種訊號之一或兩者：\n"
        "\n"
        "1. 📈 長期潛力訊號：商品因 EOL / 供給縮減 / 收藏熱度上升 / 新弾發表等理由，\n"
        "   未來價格有上漲潛力。現在以合理價購入屬於長期投資。\n"
        "2. ⚡ 立即套利訊號：restock / 抽選開放 / 限時優惠 / mispriced listing /\n"
        "   deadline 提醒 — 立即行動可撿到明顯低於市價的商品。\n"
        "\n"
        "使用者興趣檔案：\n"
        f"- 主動追蹤的商品（Mercari/Rakuma watchlist）：{_list_or_none(watchlist_queries)}\n"
        f"- 釘選為目標的 candidate：{_list_or_none(pinned_targets)}\n"
        "- 過去 30 天 SNS feedback 統計（按 rule 聚合）：\n"
        f"    👍 過此 rule 的次數：{up}\n"
        f"    💰 因此 rule 下手過：{bought}\n"
        f"    👎 過此 rule 的次數：{down}\n"
        "\n"
        "知識庫參考（針對推文中提到的 entity 自動 retrieve；可能含過時資訊，\n"
        "請以推文為主、知識庫為輔）：\n"
        f"{knowledge_block}\n"
        "\n"
        + (
            "IP 熱度指標（最新，跨 X mention / 4chan / Google Trends 的 30 日 percentile）：\n"
            f"{heat_block}\n"
            "（percentile ≥ 70 = 近期熱度明顯高於歷史均值，可對 long_term_score 加成 +5~+15）\n"
            "\n"
            if heat_block else ""
        )
        + "推文內容：\n"
        f"作者：@{author_handle}\n"
        f"時間：{created_at}\n"
        "文字：\n"
        f'"""\n{tweet_text}\n"""\n'
        "\n"
        "請嚴格輸出 JSON（不要 markdown fences、不要說明）：\n"
        "{\n"
        '  "long_term_score": 0-100,\n'
        '  "arbitrage_score": 0-100,\n'
        '  "matched_products": ["..."],\n'
        '  "matched_keywords": ["..."],\n'
        '  "suggested_action": "一句話、imperative、繁體中文",\n'
        '  "rationale": "為何給此分數、依據哪幾個句子（≤ 60 字、繁體中文）",\n'
        '  "deadline": "ISO8601 或 null",\n'
        '  "actionability": "concrete" | "vague",\n'
        '  "giveaway_spam": true | false,\n'
        '  "is_event_signal": true | false,\n'
        '  "event_name": "活動名稱 或 null",\n'
        '  "event_date": "ISO8601（活動/開賣開始日）或 原文時間字串 或 null",\n'
        '  "event_location": "地點 或 null",\n'
        '  "signup_url": "報名/購票/前売券 URL 或 null",\n'
        '  "recommended_character": "建議入手的熱門角色或限定商品；不確定填 null",\n'
        '  "purchase_target": { "sku_or_title": "...", "purchase_url": "...", "price_hint": "..." } | null\n'
        "}\n"
        "\n"
        "分數標準：\n"
        "- 0-29 = 雜訊 / 無關 / 二手評論\n"
        "- 30-59 = 弱訊號（提到的商品不在使用者興趣內、或行動意圖模糊）\n"
        "- 60-79 = 中訊號（商品 partially 在使用者興趣內，或行動意圖明確）\n"
        "- 80-100 = 強訊號（商品在 watchlist / pinned 內、且行動類型明確）\n"
        "\n"
        "兩個分數可同時高（例：「アビスアイ 1BOX 抽選販売」對長期+立即都是強訊號）。\n"
        "未提到使用者興趣內的商品，最高給到 50（除非為通用 TCG 大事件如 set EOL 公告）。\n"
        "\n"
        "**Ex-ante 事前訊號特別加權**（這些是高優先級「未來機會」訊號，即使商品還沒在使用者 watchlist）：\n"
        "- 「IP × TCG collab 公告」(例：『チェンソーマン × UNION ARENA』『鬼滅 × Weiss』)\n"
        "    → long_term_score 70-85（未來潛力）、若同時開放予約 arbitrage_score 60-80\n"
        "- 「予約開始」/「抽選販賣公告」+ deadline（例：『6/1 10:00 抽選申込開始』）\n"
        "    → arbitrage_score 70-90（立即行動）、同時 long_term_score 60-80 若 IP 知名\n"
        "- 「アニメ第 N 期発表」/「劇場版公開」/「漫畫完結倒數」/「実写化」等 IP 熱度前置訊號\n"
        "    → long_term_score 60-80（即使尚無 TCG 公告，IP 熱度先行訊號也值得追蹤）\n"
        "- 「新弾発売決定」/「ブースター発売」/「拡張パック公開」（Bushiroad / UA / Pokemon Center 公式）\n"
        "    → long_term_score 70-85、若含發售日 arbitrage_score 50-70（為 deadline 留時間）\n"
        "\n"
        "判斷時請優先看推文是否帶以上 ex-ante 訊號 — 即使商品不在使用者 watchlist，這類訊號仍應給到 60+ 分。\n"
        "\n"
        "**actionability 判定（控制是否實際推播給使用者）**：\n"
        "actionability 必須為 \"concrete\" 當且僅當推文同時包含：\n"
        "  (a) 具體 SKU 或商品標題；\n"
        "  (b) 立即可下單／申込的入口（Mercari/Rakuma URL、現正開放中的官方抽選申込 URL、\n"
        "      再販連結、或附價格的訂購頁）。\n"
        "否則一律為 \"vague\"。趨勢觀察／公告／新聞／商品介紹頁（無下單入口）／\n"
        "「新弾発表，敬請期待」這類前置訊號都算 vague。\n"
        "purchase_target 在 vague 時必為 null；concrete 時填上 SKU/標題、purchase_url、price_hint。\n"
        "vague 訊號不會推播給使用者，僅入庫做 RAG 累積，故請保守判定 — 寧可 vague。\n"
        "\n"
        "**giveaway_spam 判定（過濾中獎機率趨近於零、對使用者無價值的抽獎）**：\n"
        "核心判準＝中獎／取得機率是否趨近於零，且對使用者沒有實際可取得或可購買的價值。\n"
        "giveaway_spam = true：典型為大量轉推型『無償プレゼント企画 / 無料配布 / オリパ無料抽選』—\n"
        "任何人靠「フォロー＋リポスト（リツイート/引用）／リプ／免費應募」就能參加，動輒上萬轉推搶 1 份免費獎品，\n"
        "中獎率趨近於零；且主辦多為養粉 farming 帳號，根本無法確認是否真的會開獎/兌獎，可信度低，\n"
        "就算中了也沒有可購買的套利標的，對使用者無價值。判斷依語意不是單一字詞，\n"
        "改寫、emoji、不同說法（拡散希望 / 引リツ / フォロバ / GIVEAWAY / リプで応募 等）都算；\n"
        "重點是『免費＋海量參加＝機率趨近於零』，而非是否出現轉推這個動作本身。\n"
        "**giveaway_spam = false（不可誤刪）**：正當且實際可取得的機會 — 中選者需『付費購買』商品的店家/官方抽選，\n"
        "包含『抽選販売 / 抽選申込 / 予約抽選 / 店頭抽選』，以及透過 Livepocket（t.livepocket.jp）、L-tike、\n"
        "官網或店鋪申込フォーム 進行、當選後需入金/購入/店頭受取 的抽選。這類限量、申込制，中籤率遠高於海量免費抽，\n"
        "且是以定價買到實體商品的正當管道；即使同時要求フォロー＆RT，只要最終付費買到商品，一律 false。\n"
        "grounding：若上方知識庫參考或本 rule 的 👎 feedback 顯示此類無償抽獎已被使用者多次拒絕，請更傾向判 true。\n"
        "當 giveaway_spam = true 時，long_term_score 與 arbitrage_score 一律給 0。\n"
        "\n"
        "**is_event_signal 判定（IP 限定活動／聯名 公告 — 即使尚無購買連結也要推播）**：\n"
        "is_event_signal = true 的條件（必須同時滿足）：\n"
        "① 推文是某 IP 的『限定活動或限定聯名商品公告』，包含但不限於：\n"
        "  IP×店舗/コンビニ コラボ（例：プロセカ × Lawson）、限定アクスタ/グッズ 抽選・発売、\n"
        "  展覧会/原画展（例：名探偵プリキュア！展）、コラボカフェ、来場特典つきイベント、限定物販。\n"
        "② **地理限制（硬性條件）：活動地點必須在『日本』或『台灣』。**\n"
        "  非日本/台灣的活動（如美國 Costco、歐洲發行、英語圈 drop 等）→ is_event_signal = false。\n"
        "  判斷依據：event_location 中的地名、店名（Lawson/セブン/ローソン → 日本；全家/誠品 → 台灣；Costco USA/Target/Amazon.com → 非日台）、\n"
        "  或推文語言/帳號地區暗示（英語 Costco drop 通知 → 美國 → false）。\n"
        "  若地點不明但推文為日語且明顯是日本市場活動 → 視為日本（true）。\n"
        "這類『公告階段』即使還沒有立即下單連結，也要 is_event_signal = true（讓使用者提早準備）。\n"
        "保守：單純趨勢閒聊／一般新聞／非限定的常規商品／非日台地區活動 → 不是 event signal。\n"
        "\n"
        "**與 giveaway_spam 的界線（重要，不可混淆）**：付費入場特典／来場特典／前売券・有料チケット\n"
        "的活動（需付費、有實體會場與日期、且有可購買的限定商品）＝付費即可取得的真實價值，屬於 EVENT\n"
        "（is_event_signal = true、giveaway_spam = false），不是 spam。沿用上面的判準：趨近零機率＋無價值\n"
        "＋無法驗證主辦 才算 spam；付費活動正好相反。giveaway_spam 仍最優先，但不可吞掉付費／有實際價值的活動。\n"
        "\n"
        "**事件欄位抽取（只在推文出現時填，否則 null）**：\n"
        "- event_name：活動／展／聯名 的名稱。\n"
        "- event_date：活動或開賣的『開始日』，僅取自推文內文（會期區間取開始日，例『2026年5月15日〜6月7日』→ 2026-05-15），能轉 ISO8601 就轉。\n"
        "  ⚠️ 嚴禁把上方『時間：…UTC』那行（那是貼文發佈時間，不是活動日期）填進 event_date。內文若沒有明確活動日期（例『6月中旬』『後日発表』『日期未定』）→ 填 null，不要臆造、不要拿發文時間頂替。\n"
        "- event_location：會場／店鋪／地區。\n"
        "- signup_url：申込／抽選申込／購票／前売券 的 URL。\n"
        "\n"
        "**recommended_character（目標商品／角色，建議入手哪個）**：\n"
        "推薦『最值得轉售／最熱門、可購買的限定販售商品』——亦即 会場限定/イベント限定/コラボ限定 商品中\n"
        "最熱門角色的款式。判斷依據＝上方知識庫參考 + 推文本身內容（不得硬背清單）。\n"
        "目標是『可購買的限定商品』；免費贈品／ランダム配布／来場特典 只是附帶，絕不是轉售標的，不要當作推薦目標。\n"
        "若該活動的限定商品不分角色（例：展覧会的『会場限定グッズ』『展場限定商品』整批）：\n"
        "  → 直接填『会場限定グッズ』（或更具體的商品名）。範例：推文提到『会場限定グッズを多数販売』→ recommended_character = '会場限定グッズ'。\n"
        "  不要因為沒有指名單一角色就留空；留空只代表真的不知道推薦哪個。\n"
        "需處理子團體中的角色（例：『25時、ナイトコードで。』的『奏』）。\n"
        "**反幻覺護欄**：只能推薦推文中出現、或確實屬於該 IP 的角色／商品；禁止臆造未出現或不屬於該 IP 的角色；\n"
        "不確定時填 null；可回 2-3 名候選（格式如『A / B』）。"
    )


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _parse_classifier_response(raw: str) -> dict | None:
    """Tolerant JSON parser. Returns None on unparseable output — caller
    treats that as a conservative no-signal.
    Strips <think>...</think> blocks (qwen3-style chain-of-thought) before
    parsing so thinking-mode models work without reconfiguration."""
    if not raw:
        return None
    text = _THINK_TAG_RE.sub("", raw).strip()
    text = _JSON_FENCE_RE.sub("", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return None


def _clamp_score(value: object) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def _coerce_str_list(value: object, *, limit: int = 8) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned or cleaned.casefold() in seen:
            continue
        seen.add(cleaned.casefold())
        out.append(cleaned)
        if len(out) >= limit:
            break
    return tuple(out)


def classify_sns_signal(
    *,
    tweet_id: str,
    rule_id: str,
    author_handle: str,
    created_at: str,
    tweet_text: str,
    watchlist_queries: Sequence[str],
    pinned_targets: Sequence[str],
    feedback_for_rule: Mapping[str, int],
    knowledge_block: str,
    heat_block: str = "",
    matched_entities: tuple[str, ...],
    llm_fn: Callable[[str], str] | None,
) -> SnsPostSignal:
    """Run the LLM judge. On any failure returns zero scores so the gate
    drops the tweet (conservative — better silent than wrong-push)."""

    if llm_fn is None:
        logger.debug(
            "classifier: no llm_fn provided — returning zero scores tweet_id=%s",
            tweet_id,
        )
        return _empty_signal(tweet_id, rule_id, matched_entities)

    prompt = build_classifier_prompt(
        tweet_id=tweet_id, author_handle=author_handle, created_at=created_at,
        tweet_text=tweet_text,
        watchlist_queries=watchlist_queries, pinned_targets=pinned_targets,
        feedback_for_rule=feedback_for_rule, knowledge_block=knowledge_block,
        heat_block=heat_block,
    )
    try:
        raw = llm_fn(prompt)
    except Exception:
        logger.exception("classifier: LLM call failed tweet_id=%s rule_id=%s",
                         tweet_id, rule_id)
        return _empty_signal(tweet_id, rule_id, matched_entities)

    parsed = _parse_classifier_response(raw)
    if parsed is None:
        logger.warning(
            "classifier: failed to parse LLM JSON tweet_id=%s rule_id=%s raw=%r",
            tweet_id, rule_id, (raw or "")[:200],
        )
        return _empty_signal(tweet_id, rule_id, matched_entities)

    deadline = parsed.get("deadline")
    if not isinstance(deadline, str) or not deadline.strip() or deadline.strip().lower() == "null":
        deadline_iso = None
    else:
        deadline_iso = deadline.strip()

    actionability = "concrete" if str(parsed.get("actionability", "")).strip().lower() == "concrete" else "vague"
    purchase_target_json = _coerce_purchase_target(parsed.get("purchase_target"), actionability)
    giveaway_spam = _coerce_bool(parsed.get("giveaway_spam"))

    return SnsPostSignal(
        tweet_id=tweet_id,
        rule_id=rule_id,
        long_term_score=_clamp_score(parsed.get("long_term_score")),
        arbitrage_score=_clamp_score(parsed.get("arbitrage_score")),
        matched_products=_coerce_str_list(parsed.get("matched_products")),
        matched_keywords=_coerce_str_list(parsed.get("matched_keywords")),
        matched_entities=matched_entities,
        suggested_action=str(parsed.get("suggested_action", "")).strip()[:200],
        rationale=str(parsed.get("rationale", "")).strip()[:300],
        deadline_iso=deadline_iso,
        actionability=actionability,
        purchase_target_json=purchase_target_json,
        giveaway_spam=giveaway_spam,
        is_event_signal=_coerce_bool(parsed.get("is_event_signal")),
        event_name=_coerce_optional_str(parsed.get("event_name"), limit=200),
        event_date=_coerce_optional_str(parsed.get("event_date"), limit=120),
        event_location=_coerce_optional_str(parsed.get("event_location"), limit=200),
        signup_url=_coerce_optional_str(parsed.get("signup_url"), limit=500),
        recommended_character=_coerce_optional_str(parsed.get("recommended_character"), limit=200),
    )


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _coerce_optional_str(value: object, *, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or cleaned.lower() == "null":
        return ""
    return cleaned[:limit]


def _empty_signal(tweet_id: str, rule_id: str, matched_entities: tuple[str, ...]) -> SnsPostSignal:
    return SnsPostSignal(
        tweet_id=tweet_id, rule_id=rule_id,
        long_term_score=0, arbitrage_score=0,
        matched_products=(), matched_keywords=(),
        matched_entities=matched_entities,
        suggested_action="", rationale="(分類失敗)", deadline_iso=None,
        actionability="vague", purchase_target_json=None,
    )


def _coerce_purchase_target(value: object, actionability: str) -> str | None:
    """Keep purchase_target only when actionability=='concrete' and it has a
    http(s) purchase_url. Anything else collapses to None — silence-first."""
    if actionability != "concrete" or not isinstance(value, dict):
        return None
    url = value.get("purchase_url")
    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        return None
    cleaned = {
        "sku_or_title": str(value.get("sku_or_title") or "").strip()[:200],
        "purchase_url": url.strip()[:500],
        "price_hint":   str(value.get("price_hint") or "").strip()[:80],
    }
    try:
        return json.dumps(cleaned, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


# ── Gate decision helpers ────────────────────────────────────────────────────


def _effective_min_score(
    min_score: int,
    feedback_for_rule: Mapping[str, int] | None,
    score_floor: int = FEEDBACK_SCORE_FLOOR,
) -> int:
    """Lower the push threshold based on a rule's positive feedback.

    Conservative boost: 👍 -2, 💰 -5 per 30-day occurrence, floored at
    ``score_floor`` (default 50). 👎 does not raise the threshold here (it
    drives cooldown/auto-disable elsewhere)."""
    fb = feedback_for_rule or {}
    boost = (
        int(fb.get("up", 0)) * FEEDBACK_BOOST_PER_UP
        + int(fb.get("bought", 0)) * FEEDBACK_BOOST_PER_BOUGHT
    )
    # The floor only clamps the boost; it must never raise a caller's own
    # threshold that is already below the floor.
    floor = min(score_floor, min_score)
    return max(floor, min_score - boost)


def decide_push_reason(
    *,
    signal: SnsPostSignal,
    keyword_matched: bool,
    min_score: int = DEFAULT_MIN_SCORE_TO_PUSH,
    feedback_for_rule: Mapping[str, int] | None = None,
) -> str:
    """Return the bypass_reason that determines whether the monitor pushes.

    Values: 'giveaway_spam' / 'explicit_keyword' / 'both' / 'long_term' /
    'arbitrage' / 'event' / 'none'. Caller pushes iff the return value is none
    of {'none', 'giveaway_spam'}.

    Event gate (announce-stage): an IP-collab limited event/product
    announcement (``signal.is_event_signal``) pushes even when actionability is
    not "concrete" (no buy link yet) — it bypasses the actionability gate and,
    if no score reason fires, falls through to the 'event' reason.

    Giveaway-spam override (highest priority): when the LLM judged the post a
    worthless follow+retweet raffle (``signal.giveaway_spam``), never push —
    even if the rule's include_keyword matched. These have near-zero odds and
    no buyable item, so they override Bypass A. Recorded as 'giveaway_spam'
    for observability rather than silently collapsing to 'none'.

    Bypass A: when the rule's own include_keywords matched the tweet, always
    push regardless of LLM score or actionability. This protects the user's
    hand-set keyword filters — they opted into seeing those phrases, period.

    Actionability gate (silence-first): outside Bypass A, only "concrete"
    signals push — those with a specific SKU + a buy-now surface. Vague
    trend/announcement chatter is silenced (stored to the knowledge base
    by the monitor instead). Score gate still applies as a lower bound.

    Feedback boost: positive feedback on this rule (``feedback_for_rule``)
    lowers the score gate (down to FEEDBACK_SCORE_FLOOR), raising the push
    probability of similar concrete tweets. Does not affect Bypass A or the
    actionability gate.
    """
    if signal.giveaway_spam:
        return "giveaway_spam"
    if keyword_matched:
        return "explicit_keyword"
    if signal.actionability != "concrete" and not signal.is_event_signal:
        return "none"
    effective_min = _effective_min_score(min_score, feedback_for_rule)
    lt = signal.long_term_score >= effective_min
    arb = signal.arbitrage_score >= effective_min
    if lt and arb:
        return "both"
    if lt:
        return "long_term"
    if arb:
        return "arbitrage"
    if signal.is_event_signal:
        return "event"
    return "none"
