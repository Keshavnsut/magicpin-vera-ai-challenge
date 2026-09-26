"""Stateful challenge bot for the magicpin Vera AI Challenge."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from threading import RLock
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Vera Challenge Bot", version="1.0.0")
_logger = logging.getLogger("uvicorn.error")
_STARTED = time.time()
_contexts: dict[tuple[str, str], dict[str, Any]] = {}
_contexts_lock = RLock()
_conversations: dict[str, dict[str, Any]] = {}
_suppressed_keys: set[str] = set()
_opted_out_merchants: dict[str, float] = {}
_opted_out_customers: dict[str, float] = {}


def _name(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity") or {}
    return identity.get("owner_first_name") or identity.get("first_name") or identity.get("name") or "there"


def _merchant_name(merchant: dict[str, Any]) -> str:
    return (merchant.get("identity") or {}).get("name") or "the business"


def _active_offer(merchant: dict[str, Any], category: dict[str, Any]) -> str | None:
    offers = merchant.get("offers") or []
    for offer in offers:
        if str(offer.get("status", "active")).lower() == "active" and offer.get("title"):
            return str(offer["title"])
    for offer in category.get("offer_catalog") or []:
        if offer.get("title"):
            return str(offer["title"])
    return None


def _digest_item(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any] | None:
    payload = trigger.get("payload") or {}
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    for item in category.get("digest") or []:
        if wanted and item.get("id") == wanted:
            return item
    return next(iter(category.get("digest") or []), None)


def _has_trigger_consent(customer: dict[str, Any], kind: str) -> bool:
    scopes = set((customer.get("consent") or {}).get("scope") or [])
    allowed = {
        "recall_due": {"recall_reminders", "appointment_reminders"},
        "appointment_tomorrow": {"appointment_reminders"},
        "chronic_refill_due": {"refill_reminders", "recall_alerts"},
        "customer_lapsed_soft": {"winback_offers", "promotional_offers"},
        "customer_lapsed_hard": {"winback_offers", "promotional_offers"},
        "trial_followup": {"appointment_reminders", "kids_program_updates"},
        "wedding_package_followup": {"bridal_package_followup"},
    }.get(kind)
    return bool(scopes and allowed and scopes.intersection(allowed))


def _fallback_compose(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any] | None) -> dict[str, str]:
    """Grounded no-network fallback; every inserted detail comes from an input context."""
    kind = str(trigger.get("kind") or "")
    payload = trigger.get("payload") or {}
    who = _name(merchant)
    biz = _merchant_name(merchant)
    identity = merchant.get("identity") or {}
    performance = merchant.get("performance") or {}
    offer = _active_offer(merchant, category)
    item = _digest_item(category, trigger)
    source = item.get("source") if item else None
    title = item.get("title") if item else None
    cta = "open_ended"

    if customer:
        ci = customer.get("identity") or {}
        first = ci.get("name") or "there"
        lang = str(ci.get("language_pref") or "").lower()
        hi = "Apke liye" if "hi" in lang else "For you"
        consent = customer.get("consent") or {}
        allowed = consent.get("scope") or []
        if not allowed:
            return {"body": "", "cta": "none", "rationale": "Customer consent scope is absent; no customer message composed."}
        slots = payload.get("available_slots") or payload.get("next_session_options") or []
        slot_labels = [str(s.get("label")) for s in slots if s.get("label")]
        state = customer.get("state", "")
        if kind in {"recall_due", "customer_lapsed_soft", "customer_lapsed_hard", "chronic_refill_due", "trial_followup", "wedding_package_followup"}:
            service = payload.get("service_due") or payload.get("intent_topic") or "follow-up"
            detail = f"Your {str(service).replace('_', ' ')} is due. " if kind == "recall_due" else ""
            body = f"Hi {first}, {biz} here. {detail}"
            if payload.get("days_since_last_visit"):
                body += f"It's been {payload['days_since_last_visit']} days since your last visit; no pressure. "
            if slot_labels:
                body += f"{hi}: {', '.join(slot_labels)}. "
            if offer:
                body += f"Current offer: {offer}. "
            body += "Would you like us to help arrange a visit?"
            cta = "binary_yes_no"
            return {"body": body, "cta": cta, "rationale": f"Customer follow-up based on the {kind} trigger, available schedule and consented contact scope."}
        return {"body": f"Hi {first}, a quick update from {biz}. Reply if you'd like us to help.", "cta": "open_ended", "rationale": f"Customer message is limited to the supplied {kind} context."}

    if kind in {"research_digest", "regulation_change", "cde_opportunity", "supply_alert"}:
        if kind == "supply_alert":
            batches = ", ".join(payload.get("affected_batches") or [])
            body = f"{who}, a supply alert names {payload.get('molecule', 'a product')} batches {batches} from {payload.get('manufacturer', 'the listed manufacturer')}."
            cta = "binary_yes_no"
            ask = "Want me to prepare a customer notice for your review?"
        elif item:
            body = f"{who}, {title}."
            if payload.get("deadline_iso"):
                body += f" The stated effective date is {payload['deadline_iso']}."
            if source:
                body += f" Source: {source}."
            ask = "Want me to prepare a short summary and a draft you can review?"
        else:
            body = f"{who}, there is a new {kind.replace('_', ' ')} update for {category.get('display_name', 'your category')} ."
            ask = "Want me to summarize the supplied details?"
        body += " " + ask
    elif kind in {"perf_dip", "seasonal_perf_dip", "perf_spike"}:
        metric = payload.get("metric") or "views"
        delta = payload.get("delta_pct")
        if delta is None:
            delta = ((performance.get("delta_7d") or {}).get(f"{metric}_pct"))
        change = f"{abs(float(delta)):.0%}" if isinstance(delta, (int, float)) else ""
        direction = "down" if isinstance(delta, (int, float)) and delta < 0 else "up"
        body = f"{who}, your {metric} are {direction}{(' ' + change) if change else ''} over {payload.get('window', 'the reported period')} according to this trigger."
        if payload.get("is_expected_seasonal") and payload.get("season_note"):
            body += f" The context flags this as seasonal: {payload['season_note'].replace('_', ' ')}."
        body += " Want me to outline one practical next step?"
    elif kind == "renewal_due":
        body = f"{who}, your {payload.get('plan', 'current')} plan renewal is coming up in {payload.get('days_remaining', 'a few')} days."
        if payload.get("renewal_amount") is not None:
            body += f" The listed renewal amount is ₹{payload['renewal_amount']}."
        body += " Would you like the renewal details?"
    elif kind == "active_planning_intent":
        topic = str(payload.get("intent_topic") or "your idea").replace("_", " ")
        body = f"{who}, let's turn your {topic} idea into a first draft."
        if offer:
            body += f" I can start from the current offer, {offer}."
        body += " Shall I draft the plan now?"
        cta = "binary_yes_no"
    elif kind == "ipl_match_today":
        body = f"{who}, {payload.get('match', 'the match')} is scheduled at {payload.get('match_time_iso', 'the time in your trigger')} at {payload.get('venue', 'the listed venue')}."
        if offer:
            body += f" Your active offer is {offer}."
        body += " Want me to draft a match-day post using only that offer?"
    elif kind == "review_theme_emerged":
        body = f"{who}, {payload.get('occurrences_30d', 'Several')} recent reviews mention {str(payload.get('theme', 'a recurring theme')).replace('_', ' ')}."
        if payload.get("trend"):
            body += f" The reported trend is {payload['trend']}."
        body += " Want a short response draft for your review?"
    elif kind == "milestone_reached":
        body = f"{who}, you're approaching {payload.get('milestone_value', 'the next')} {str(payload.get('metric', 'milestone')).replace('_', ' ')}; the current value is {payload.get('value_now', 'in the trigger')}. Want a simple post draft to mark it?"
    elif kind == "curious_ask_due":
        body = f"Hi {who}, quick question: what service has customers asked about most this week? I can turn your answer into a short post."
    elif kind == "festival_upcoming":
        body = f"{who}, {payload.get('festival', 'the upcoming festival')} is listed for {payload.get('date', 'the date in your trigger')}."
        if offer:
            body += f" You already have {offer}."
        body += " Want a timely post draft built around it?"
    elif kind == "competitor_opened":
        body = f"{who}, {payload.get('competitor_name', 'a nearby competitor')} is listed {payload.get('distance_km', 'nearby')} km away, with {payload.get('their_offer', 'an offer in the trigger')}. Want to review how your current offer compares?"
    elif kind == "dormant_with_vera":
        body = f"Hi {who}, it's been {payload.get('days_since_last_merchant_message', 'a while')} days since we last spoke about {str(payload.get('last_topic', 'your account')).replace('_', ' ')}. Is there one thing you'd like help with this week?"
    elif kind == "gbp_unverified":
        body = f"{who}, your Google Business Profile is marked unverified in the latest trigger. The listed path is {str(payload.get('verification_path', 'the provided verification process')).replace('_', ' ')}. Want me to walk you through the next step?"
    elif kind == "category_seasonal":
        trends = ", ".join(str(x).replace("_", " ") for x in payload.get("trends", [])[:3])
        body = f"{who}, the seasonal update flags these demand shifts: {trends or payload.get('season', 'seasonal changes')}. Want a short shelf or post checklist based on this?"
    elif kind == "winback_eligible":
        body = f"{who}, since your plan expired {payload.get('days_since_expiry', 'several')} days ago, the trigger reports {payload.get('lapsed_customers_added_since_expiry', 'some')} additional lapsed customers. Want to review a practical re-engagement idea?"
    else:
        body = f"{who}, I have an update about {kind.replace('_', ' ') or 'your business'} based on the latest trigger. Want me to summarize the next useful step?"

    return {"body": re.sub(r"\s+", " ", body).strip(), "cta": cta, "rationale": f"Composed for the {kind or 'current'} trigger using the supplied merchant and {category.get('slug', 'category')} context; no external facts added."}


def _llm_settings() -> tuple[str, str, str] | None:
    """Return compatible chat-completions settings for the configured provider."""
    provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    if not provider:
        if os.getenv("GROQ_API_KEY"):
            provider = "groq"
        elif os.getenv("GEMINI_API_KEY"):
            provider = "gemini"
        else:
            provider = "openai"
    if provider == "groq":
        key = os.getenv("GROQ_API_KEY")
        return (key, os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"), "https://api.groq.com/openai/v1/chat/completions") if key else None
    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY")
        return (key, os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions") if key else None
    if provider == "openai":
        key = os.getenv("OPENAI_API_KEY")
        return (key, os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "https://api.openai.com/v1/chat/completions") if key else None
    return None


def _llm_provider_name(endpoint: str) -> str:
    if "api.groq.com" in endpoint:
        return "groq"
    if "generativelanguage.googleapis.com" in endpoint:
        return "gemini"
    return "openai"


def _openai_compose(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any] | None, fallback: dict[str, str]) -> dict[str, str]:
    settings = _llm_settings()
    if not settings:
        _logger.warning("llm_compose result=fallback reason=provider_or_api_key_missing")
        return fallback
    key, model, endpoint = settings
    provider = _llm_provider_name(endpoint)
    system = (
        "You compose concise WhatsApp messages for the synthetic magicpin Vera challenge. "
        "Use only facts present in the JSON contexts. Never invent prices, dates, slots, sources, metrics, actions, "
        "availability, policies, or customer data. Match category voice and language. Respect voice.vocab_taboo. "
        "For customer-facing messages require explicit consent scope for this trigger. Use one primary CTA. "
        "Never include URLs. Do not claim an action has already happened. Return only JSON with string keys body, cta, rationale. "
        "Allowed cta values: none, open_ended, binary_yes_no, multi_choice_slot. Keep the rationale aligned with the body."
    )
    context = {"category": category, "merchant": merchant, "trigger": trigger, "customer": customer}
    request_body = json.dumps({
        "model": model, "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": "Compose from these contexts. The deterministic baseline is included only as a quality reference; correct it if context requires.\n" + json.dumps({"contexts": context, "baseline": fallback}, ensure_ascii=False)}]
    }, ensure_ascii=False).encode("utf-8")
    req = Request(endpoint, data=request_body,
                  headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "8"))) as response:
            raw = json.loads(response.read().decode("utf-8"))
        result = json.loads(raw["choices"][0]["message"]["content"])
        body = str(result.get("body", "")).strip()
        cta = str(result.get("cta", "open_ended"))
        rationale = str(result.get("rationale", "")).strip()
        taboo = [str(x).lower() for x in (category.get("voice") or {}).get("vocab_taboo", [])]
        if not body or len(body) > 1800 or re.search(r"https?://|www\.", body, re.I) or any(t and t in body.lower() for t in taboo):
            _logger.warning("llm_compose provider=%s model=%s result=fallback reason=output_validation_failed", provider, model)
            return fallback
        if cta not in {"none", "open_ended", "binary_yes_no", "multi_choice_slot"}:
            cta = fallback["cta"]
        _logger.info("llm_compose provider=%s model=%s result=success", provider, model)
        return {"body": body, "cta": cta, "rationale": rationale or fallback["rationale"]}
    except HTTPError as exc:
        _logger.warning("llm_compose provider=%s model=%s result=fallback reason=http_error status=%d", provider, model, exc.code)
        return fallback
    except (URLError, TimeoutError) as exc:
        _logger.warning("llm_compose provider=%s model=%s result=fallback reason=transport_error error_type=%s", provider, model, type(exc).__name__)
        return fallback
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        _logger.warning("llm_compose provider=%s model=%s result=fallback reason=invalid_response error_type=%s", provider, model, type(exc).__name__)
        return fallback


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict[str, str]:
    """Compose one grounded message from category, merchant, trigger, and optional customer contexts."""
    fallback = _fallback_compose(category or {}, merchant or {}, trigger or {}, customer)
    return _openai_compose(category or {}, merchant or {}, trigger or {}, customer, fallback)


class ContextPush(BaseModel):
    scope: str
    context_id: str
    version: int = Field(ge=1)
    payload: dict[str, Any]
    delivered_at: str | None = None


class TickRequest(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str | None = None
    turn_number: int = 1


def _stored(scope: str, key: str) -> dict[str, Any] | None:
    with _contexts_lock:
        item = _contexts.get((scope, key))
    return item["payload"] if item else None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@app.post("/v1/context")
def push_context(body: ContextPush):
    if body.scope not in {"category", "merchant", "customer", "trigger"}:
        raise HTTPException(status_code=400, detail={"accepted": False, "reason": "invalid_scope"})
    key = (body.scope, body.context_id)
    with _contexts_lock:
        current = _contexts.get(key)
        if current and current["version"] >= body.version:
            return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": current["version"]})
        _contexts[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _iso_now()}


def _template(kind: str, customer: dict[str, Any] | None) -> str:
    if customer:
        return "merchant_customer_update_v1" if kind not in {"recall_due", "chronic_refill_due"} else "merchant_recall_reminder_v1"
    return {
        "research_digest": "vera_research_digest_v1", "regulation_change": "vera_compliance_alert_v1",
        "supply_alert": "vera_supply_alert_v1", "recall_due": "merchant_recall_reminder_v1",
    }.get(kind, "vera_merchant_update_v1")


def _template_params(message: dict[str, str], merchant: dict[str, Any], customer: dict[str, Any] | None) -> list[str]:
    if customer:
        return [customer.get("identity", {}).get("name", "there"), _merchant_name(merchant), message["body"][:900]]
    return [_name(merchant), message["body"][:900]]


@app.post("/v1/tick")
def tick(body: TickRequest):
    actions: list[dict[str, Any]] = []
    now = datetime.fromisoformat(body.now.replace("Z", "+00:00"))
    for trigger_id in body.available_triggers:
        trigger = _stored("trigger", trigger_id)
        if not trigger or len(actions) >= 20:
            continue
        expiry = trigger.get("expires_at")
        if expiry:
            try:
                if datetime.fromisoformat(expiry.replace("Z", "+00:00")) < now:
                    continue
            except ValueError:
                pass
        merchant_id = trigger.get("merchant_id")
        merchant = _stored("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue
        if _opted_out_merchants.get(merchant_id, 0) > time.time():
            continue
        category = _stored("category", merchant.get("category_slug", ""))
        if not category:
            continue
        customer = None
        customer_id = trigger.get("customer_id")
        if trigger.get("scope") == "customer":
            customer = _stored("customer", customer_id) if customer_id else None
            if not customer or customer.get("merchant_id") != merchant_id:
                continue
            if _opted_out_customers.get(customer_id, 0) > time.time():
                continue
            consent = customer.get("consent") or {}
            if not _has_trigger_consent(customer, str(trigger.get("kind", ""))):
                continue
        suppression = str(trigger.get("suppression_key") or trigger_id)
        if suppression in _suppressed_keys:
            continue
        message = compose(category, merchant, trigger, customer)
        if not message["body"]:
            continue
        conversation_id = f"conv_{merchant_id}_{customer_id or trigger_id}"
        if conversation_id in _conversations:
            continue
        action = {
            "conversation_id": conversation_id, "merchant_id": merchant_id, "customer_id": customer_id,
            "send_as": "merchant_on_behalf" if customer else "vera", "trigger_id": trigger_id,
            "template_name": _template(str(trigger.get("kind", "")), customer),
            "template_params": _template_params(message, merchant, customer),
            "body": message["body"], "cta": message["cta"],
            "suppression_key": suppression, "rationale": message["rationale"],
        }
        actions.append(action)
        _suppressed_keys.add(suppression)
        _conversations[conversation_id] = {
            "merchant_id": merchant_id, "customer_id": customer_id, "category": category,
            "merchant": merchant, "customer": customer, "trigger": trigger,
            "turns": [{"role": "vera", "body": message["body"]}],
            "auto_reply_count": 0, "ended": False, "last_reply_at": None,
        }
    return {"actions": actions}


def _is_opt_out(text: str) -> bool:
    return bool(re.search(r"\b(stop|unsubscribe|don't message|do not message|not interested|no more messages)\b", text, re.I))


def _is_auto_reply(text: str) -> bool:
    normalized = re.sub(r"\W+", " ", text.lower()).strip()
    patterns = ("thank you for contacting", "thanks for contacting", "our team will respond", "team will get back",
                "automated assistant", "working hours", "hamari team tak", "jaankari ke liye bahut")
    return any(p in normalized for p in patterns)


def _explicit_intent(text: str) -> bool:
    return bool(re.search(r"\b(yes|yeah|yep|let'?s do it|go ahead|proceed|sign me up|i want to join|start now|do it)\b", text, re.I))


@app.post("/v1/reply")
def reply(body: ReplyRequest):
    conv = _conversations.get(body.conversation_id)
    if not conv:
        return {"action": "end", "rationale": "No active conversation exists for this reply."}
    if conv.get("ended"):
        return {"action": "end", "rationale": "This conversation has already ended."}
    # Adaptive judge injections replace old context versions; reply using the latest snapshots.
    latest_merchant = _stored("merchant", conv["merchant_id"])
    if latest_merchant:
        conv["merchant"] = latest_merchant
        latest_category = _stored("category", latest_merchant.get("category_slug", ""))
        if latest_category:
            conv["category"] = latest_category
    if conv.get("customer_id"):
        latest_customer = _stored("customer", conv["customer_id"])
        if latest_customer:
            conv["customer"] = latest_customer
    latest_trigger = _stored("trigger", conv["trigger"].get("id", ""))
    if latest_trigger:
        conv["trigger"] = latest_trigger
    text = body.message.strip()
    conv["turns"].append({"role": body.from_role, "body": text})
    conv["last_reply_at"] = body.received_at
    merchant_id = body.merchant_id or conv["merchant_id"]
    def record_opt_out() -> None:
        if body.from_role == "customer" and conv.get("customer_id"):
            _opted_out_customers[conv["customer_id"]] = time.time() + 365 * 86400
        else:
            _opted_out_merchants[merchant_id] = time.time() + 30 * 86400
    if _is_opt_out(text):
        conv["ended"] = True
        record_opt_out()
        return {"action": "end", "rationale": "The recipient asked to stop; ending and suppressing the relevant outreach."}
    if _is_auto_reply(text):
        conv["auto_reply_count"] += 1
        if conv["auto_reply_count"] == 1:
            return {"action": "send", "body": "Looks like this may be an automated reply. When the owner or manager sees this, reply YES if they'd like to continue.", "cta": "binary_yes_no", "rationale": "Detected likely canned auto-reply and allowed one brief handoff attempt."}
        if conv["auto_reply_count"] == 2:
            return {"action": "wait", "wait_seconds": 86400, "rationale": "A repeated canned response suggests no person is available; backing off for 24 hours."}
        conv["ended"] = True
        return {"action": "end", "rationale": "Repeated auto-replies show no human engagement; closing the conversation."}
    if re.search(r"\b(stop|useless|bothering me|angry|leave me alone)\b", text, re.I):
        conv["ended"] = True
        record_opt_out()
        return {"action": "end", "rationale": "The merchant is frustrated; closing without another pitch."}
    if _explicit_intent(text):
        conv["auto_reply_count"] = 0
        trigger = conv["trigger"]
        kind = trigger.get("kind", "")
        if kind == "active_planning_intent":
            topic = str((trigger.get("payload") or {}).get("intent_topic", "your idea")).replace("_", " ")
            msg = f"Great — I'll draft a first version for {topic} using the details already shared. Reply CONFIRM when you're ready for me to prepare the final copy."
        elif kind in {"research_digest", "cde_opportunity"}:
            msg = "Great — I'll prepare the requested summary and a concise draft from the supplied source details."
        else:
            msg = "Great — I'll prepare the next step using the details already in this conversation. If anything needs your approval, I'll show you the draft first."
        conv["turns"].append({"role": "vera", "body": msg})
        return {"action": "send", "body": msg, "cta": "binary_yes_no", "rationale": "Recognized explicit intent and moved directly to an actionable next step without another qualification question."}
    if len(conv["turns"]) >= 7:
        conv["ended"] = True
        return {"action": "end", "rationale": "Conversation reached the turn limit; ending to avoid over-messaging."}
    if conv.get("customer_id"):
        customer = conv.get("customer") or {}
        first = (customer.get("identity") or {}).get("name") or "there"
        return {"action": "send", "body": f"Thanks, {first}. I'll note that and share the next step with the clinic team.", "cta": "none", "rationale": "Acknowledged the customer's reply without making a medical or service claim."}
    if re.search(r"\b(gst|tax filing|unrelated|different topic)\b", text, re.I):
        return {"action": "send", "body": "I can't help with that request here. I can continue with the update we were discussing if useful.", "cta": "open_ended", "rationale": "Briefly declined an unrelated request and redirected to the original trigger."}
    result = _openai_reply(conv, text)
    conv["turns"].append({"role": "vera", "body": result["body"]})
    return {"action": "send", **result}


def _openai_reply(conv: dict[str, Any], latest: str) -> dict[str, str]:
    settings = _llm_settings()
    if not settings:
        _logger.warning("llm_reply result=fallback reason=provider_or_api_key_missing")
        return {"body": "Thanks — I'll use that detail in the draft. Is there anything specific you'd like included?", "cta": "open_ended", "rationale": "Acknowledged the merchant's response and advanced the original task."}
    key, model, endpoint = settings
    provider = _llm_provider_name(endpoint)
    system = "Continue this synthetic merchant support conversation. Honor explicit requests, do not invent facts or claim work was completed, respect category taboos, and keep the next step concise. Return JSON: body, cta, rationale. No URLs."
    data = {"model": model, "temperature": 0, "response_format": {"type": "json_object"}, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps({"category": conv["category"], "merchant": conv["merchant"], "trigger": conv["trigger"], "conversation": conv["turns"], "latest_reply": latest}, ensure_ascii=False)}
    ]}
    req = Request(endpoint, data=json.dumps(data, ensure_ascii=False).encode(),
                  headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "8"))) as resp:
            raw = json.loads(resp.read().decode())
        result = json.loads(raw["choices"][0]["message"]["content"])
        body = str(result.get("body", "")).strip()
        if not body or re.search(r"https?://|www\.", body, re.I):
            raise ValueError("invalid completion")
        _logger.info("llm_reply provider=%s model=%s result=success", provider, model)
        return {"body": body, "cta": str(result.get("cta", "open_ended")), "rationale": str(result.get("rationale", "Continued the conversation using its current context."))}
    except HTTPError as exc:
        _logger.warning("llm_reply provider=%s model=%s result=fallback reason=http_error status=%d", provider, model, exc.code)
        return {"body": "Thanks — I'll use that detail in the draft. Is there anything specific you'd like included?", "cta": "open_ended", "rationale": "Acknowledged the merchant's response and advanced the original task."}
    except (URLError, TimeoutError) as exc:
        _logger.warning("llm_reply provider=%s model=%s result=fallback reason=transport_error error_type=%s", provider, model, type(exc).__name__)
        return {"body": "Thanks — I'll use that detail in the draft. Is there anything specific you'd like included?", "cta": "open_ended", "rationale": "Acknowledged the merchant's response and advanced the original task."}
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        _logger.warning("llm_reply provider=%s model=%s result=fallback reason=invalid_response error_type=%s", provider, model, type(exc).__name__)
        return {"body": "Thanks — I'll use that detail in the draft. Is there anything specific you'd like included?", "cta": "open_ended", "rationale": "Acknowledged the merchant's response and advanced the original task."}


@app.get("/v1/healthz")
def healthz():
    counts = {scope: 0 for scope in ("category", "merchant", "customer", "trigger")}
    with _contexts_lock:
        context_keys = list(_contexts)
    for scope, _ in context_keys:
        counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time() - _STARTED), "contexts_loaded": counts}


@app.get("/v1/metadata")
def metadata():
    members = [x.strip() for x in os.getenv("TEAM_MEMBERS", "").split(",") if x.strip()]
    llm_settings = _llm_settings()
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera Challenge Participant"),
        "team_members": members,
        "model": llm_settings[1] if llm_settings else "deterministic-fallback",
        "approach": "Context-grounded trigger routing with deterministic safeguards and optional OpenAI-compatible composition",
        "contact_email": os.getenv("CONTACT_EMAIL", ""),
        "version": os.getenv("APP_VERSION", "1.0.0"),
        "submitted_at": os.getenv("SUBMITTED_AT", ""),
    }


@app.post("/v1/teardown")
def teardown():
    with _contexts_lock:
        _contexts.clear()
    _conversations.clear()
    _suppressed_keys.clear()
    _opted_out_merchants.clear()
    _opted_out_customers.clear()
    return {"cleared": True}
