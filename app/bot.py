from __future__ import annotations

import os, re, time, uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

APP_VERSION = "1.0.0"
START = time.time()

# In-memory state is intentional: challenge says persistence for the test is enough.
contexts: Dict[str, Dict[str, Dict[str, Any]]] = {
    "category": {}, "merchant": {}, "customer": {}, "trigger": {}
}
conversations: Dict[str, Dict[str, Any]] = {}
sent_suppressions: set[str] = set()

app = FastAPI(title="Vera Merchant AI Assistant", version=APP_VERSION)

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


def _name(merchant: dict) -> str:
    return merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "there")


def _merchant_name(merchant: dict) -> str:
    return merchant.get("identity", {}).get("name", "your business")


def _pct(x: Any) -> str:
    try:
        return f"{float(x) * 100:.0f}%"
    except Exception:
        return str(x)


def _active_offer(merchant: dict, category: dict | None = None) -> Optional[str]:
    for o in merchant.get("offers", []) or []:
        if o.get("status") == "active" and o.get("title"):
            return o["title"]
    for o in (category or {}).get("offer_catalog", []) or []:
        if isinstance(o, dict) and o.get("title"):
            return o["title"]
        if isinstance(o, str):
            return o
    return None


def _lang_mix(merchant: dict) -> bool:
    langs = merchant.get("identity", {}).get("languages", []) or []
    return "hi" in langs and "en" in langs


def _customer_allowed(customer: dict, trigger: dict) -> bool:
    prefs = customer.get("preferences", {}) or {}
    consent = customer.get("consent", {}) or {}
    if prefs.get("channel") and prefs.get("channel") != "whatsapp":
        return False
    if prefs.get("reminder_opt_in") is False:
        return False
    scopes = consent.get("scope", []) or []
    kind = trigger.get("kind", "")
    if kind == "recall_due" and scopes and "recall_reminders" not in scopes:
        return False
    if kind in {"appointment_due", "appointment_reminder"} and scopes and "appointment_reminders" not in scopes:
        return False
    return True


def _trigger_payload(trigger: dict) -> dict:
    return trigger.get("payload", {}) or {}


def _compose_merchant(category: dict, merchant: dict, trigger: dict) -> dict:
    kind = trigger.get("kind", "unknown")
    p = _trigger_payload(trigger)
    first = _name(merchant)
    business = _merchant_name(merchant)
    cat = merchant.get("category_slug") or category.get("slug", "business")
    offer = _active_offer(merchant, category)
    perf = merchant.get("performance", {}) or {}
    delta = perf.get("delta_7d", {}) or {}
    signals = merchant.get("signals", []) or []
    lang = _lang_mix(merchant)

    cta = "open_ended"
    rationale = "Uses the active trigger with merchant-specific context."
    template = f"vera_{kind}_v1"
    params: list[Any] = [first]

    if kind in {"perf_dip", "performance_dip"}:
        metric = p.get("metric", "performance")
        d = p.get("delta_pct", delta.get(metric + "_pct"))
        baseline = p.get("vs_baseline")
        if d is not None:
            change = _pct(abs(float(d)))
            direction = "down"
            detail = f"{metric} are down {change}"
            if baseline is not None:
                detail += f" vs a baseline of {baseline}"
        else:
            detail = "your recent performance has dipped"
        body = f"{first}, {detail}."
        if offer:
            body += f" You already have {offer} active. Want me to suggest a focused way to use it?"
        else:
            body += " Want me to help identify the most relevant next action?"
        rationale = f"Performance-dip trigger for {business}; anchored to the reported metric and current offer state."

    elif kind in {"perf_spike", "performance_spike"}:
        metric = p.get("metric", "performance")
        d = p.get("delta_pct")
        change = _pct(abs(float(d))) if d is not None else ""
        body = f"{first}, {metric} are up {change} this period." if change else f"{first}, your {metric} are trending up."
        if offer:
            body += f" Your {offer} is already live — want to build on the momentum?"
        else:
            body += " Want me to suggest one concrete way to build on the momentum?"
        rationale = "Positive performance trigger paired with a concrete merchant action."

    elif kind in {"renewal_due", "renewal_expiring"}:
        days = p.get("days_remaining", merchant.get("subscription", {}).get("days_remaining"))
        amount = p.get("renewal_amount")
        plan = p.get("plan", merchant.get("subscription", {}).get("plan"))
        body = f"{first}, your {plan or 'plan'} renews in {days} days" if days is not None else f"{first}, your subscription is nearing renewal"
        if amount:
            body += f" at ₹{amount}."
        else:
            body += "."
        body += " Want me to walk you through the renewal?"
        rationale = "Urgent renewal trigger; includes the supplied days/price rather than a generic reminder."
        cta = "YES/STOP"

    elif kind in {"recall_due", "appointment_due", "appointment_reminder"} and trigger.get("scope") == "customer":
        # Customer scope is handled by _compose_customer; this branch is a safe fallback.
        body = f"{first}, a customer follow-up is due. Want me to draft it?"
        rationale = "Customer-scoped trigger received without a resolvable customer context."

    elif kind in {"research_digest", "research_digest_release"}:
        top = p.get("top_item") or {}
        if not top:
            top_id = p.get("top_item_id")
            for item in category.get("digest", []) or []:
                if item.get("id") == top_id:
                    top = item
                    break
            if not top and category.get("digest"):
                top = category["digest"][0]
        title = top.get("title") or p.get("headline") or "a new category research update"
        source = top.get("source")
        body = f"{first}, a new {cat} research update caught my eye: {title}"
        if source:
            body += f" ({source})"
        body += ". Worth a look?"
        rationale = "Research trigger framed as a category-relevant knowledge hook, with source attribution when supplied."
        cta = "open_ended"

    elif kind in {"regulation_change", "compliance_change"}:
        deadline = p.get("deadline_iso") or p.get("deadline")
        top = p.get("top_item") or {}
        title = top.get("title") or p.get("headline") or "a compliance update"
        body = f"{first}, there’s a new {cat} compliance update: {title}"
        if deadline:
            body += f" Deadline: {deadline}."
        else:
            body += "."
        body += " Want me to summarise what changed?"
        rationale = "Compliance trigger prioritised because it is time-sensitive and directly category-specific."
        cta = "open_ended"

    elif kind in {"festival_upcoming", "festival", "ipl_match_today", "local_event"}:
        event = p.get("festival") or p.get("match") or p.get("event") or "an upcoming local event"
        when = p.get("date") or p.get("match_time_iso")
        body = f"{first}, {event} is coming up"
        if when:
            body += f" ({when})"
        body += f". For your {cat} business, want me to suggest one relevant offer/content angle?"
        rationale = "External event matched to the merchant's category rather than sending a generic promotion."

    elif kind in {"curious_ask_due", "knowledge_nudge"}:
        ask = p.get("ask_template", "a useful category trend")
        body = f"{first}, I spotted something useful for {business}: {ask.replace('_', ' ')}. Want the quick version?"
        rationale = "Curiosity-driven nudge designed to create a low-effort reply."

    elif kind in {"winback_eligible", "dormancy", "customer_lapse_threshold"}:
        dip = p.get("perf_dip_pct")
        lapsed = p.get("lapsed_customers_added_since_expiry")
        body = f"{first}, you’re eligible for a win-back push"
        if lapsed is not None:
            body += f" with {lapsed} recently lapsed customers in the signal"
        if dip is not None:
            body += f" and a {_pct(abs(float(dip)))} performance dip"
        body += ". Want me to draft a targeted message?"
        rationale = "Win-back trigger uses available lapse/performance evidence and asks for one concrete next step."

    elif kind in {"profile_incomplete", "stale_posts", "gbp_issue"} or any("stale_posts" in str(s) for s in signals):
        stale = next((s for s in signals if "stale_posts" in str(s)), None)
        body = f"{first}, your Google profile has a content gap"
        if stale:
            body += f" ({stale.replace(':', ' ')})."
        else:
            body += "."
        body += " Want me to draft the next post around your strongest current offer?" if offer else " Want me to suggest the next profile fix?"
        rationale = "Profile/content signal translated into a specific, low-effort action."

    else:
        # Generic but still grounded in the trigger itself; never invents facts.
        body = f"{first}, I have a {kind.replace('_', ' ')} update for {business}. Want the relevant next step?"
        rationale = f"Fallback composer grounded only in supplied trigger kind={kind}."

    if lang and "hi" in str(merchant.get("identity", {}).get("languages", [])):
        # Keep English-first for reliability; natural code-mix only when short.
        pass

    return {
        "body": body.strip(),
        "cta": cta,
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key", f"{kind}:{merchant.get('merchant_id','unknown')}"),
        "rationale": rationale,
        "template_name": template,
        "template_params": params,
    }


def _compose_customer(category: dict, merchant: dict, trigger: dict, customer: dict) -> dict:
    kind = trigger.get("kind", "unknown")
    p = _trigger_payload(trigger)
    name = customer.get("identity", {}).get("name", "there")
    business = _merchant_name(merchant)
    state = customer.get("state", "active")
    rel = customer.get("relationship", {}) or {}
    prefs = customer.get("preferences", {}) or {}

    if not _customer_allowed(customer, trigger):
        return {"skip": True, "reason": "customer is not eligible under supplied channel/consent context"}

    if kind == "recall_due":
        service = p.get("service_due", "follow-up")
        slots = p.get("available_slots", []) or []
        slot_text = " or ".join(s.get("label", "") for s in slots[:2] if s.get("label"))
        body = f"Hi {name}, {business} here. Your {service.replace('_', ' ')} follow-up is due."
        if slot_text:
            body += f" We have {slot_text}. Would either work for you?"
        else:
            body += " Would you like us to share available slots?"
        cta = "open_ended"
        rationale = "Recall reminder matched to the customer's supplied service history, due date and available slots."
    elif kind in {"wedding_package_followup", "bridal_followup"}:
        wedding = p.get("wedding_date")
        next_step = p.get("next_step_window_open", "the next preparation step")
        body = f"Hi {name}, following up from your earlier visit to {business}."
        if wedding:
            body += f" Your wedding is on {wedding}; the next suggested step is {next_step.replace('_', ' ')}."
        else:
            body += f" The next suggested step is {next_step.replace('_', ' ')}."
        body += " Would you like the details?"
        cta = "open_ended"
        rationale = "Customer follow-up uses the stored relationship event and stated next-step window."
    else:
        services = rel.get("services_received", []) or []
        last_service = services[-1] if services else "recent visit"
        body = f"Hi {name}, {business} here. We noticed your last {last_service.replace('_', ' ')} visit."
        if state.startswith("lapsed"):
            body += " Would you like us to share a convenient time to come back?"
        else:
            body += " Can we help with your next visit?"
        cta = "open_ended"
        rationale = "Customer message grounded in supplied relationship state and service history."

    return {
        "body": body,
        "cta": cta,
        "send_as": "merchant_on_behalf",
        "suppression_key": trigger.get("suppression_key", f"{kind}:{customer.get('customer_id','unknown')}"),
        "rationale": rationale,
        "template_name": f"merchant_{kind}_v1",
        "template_params": [name, business],
    }


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    if trigger.get("scope") == "customer" or customer is not None:
        if customer is None:
            return {"skip": True, "reason": "customer context missing for customer-scoped trigger"}
        return _compose_customer(category, merchant, trigger, customer)
    return _compose_merchant(category, merchant, trigger)


def _already_active(conversation_id: str) -> bool:
    c = conversations.get(conversation_id)
    return bool(c and c.get("active", False))


def _is_auto_reply(msg: str) -> bool:
    s = msg.lower().strip()
    patterns = [
        "thank you for contacting", "thanks for contacting", "currently unavailable",
        "will get back to you", "we will get back", "office hours", "auto reply",
        "automated response", "do not reply to this message", "we're currently away",
    ]
    return any(p in s for p in patterns) and len(s) < 500


def _intent(msg: str) -> str:
    s = msg.lower().strip()
    if any(x in s for x in ["stop", "not interested", "don't want", "do not want", "no thanks", "remove me"]):
        return "negative"
    if any(x in s for x in ["yes", "sure", "okay", "ok", "do it", "go ahead", "send it", "join", "interested"]):
        return "positive"
    if any(x in s for x in ["later", "tomorrow", "call me", "give me time", "busy", "after some time"]):
        return "wait"
    if "?" in s or any(x in s for x in ["how much", "what", "when", "where", "how does", "can you explain"]):
        return "question"
    return "neutral"


@app.get("/v1/healthz")
def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": {k: len(v) for k, v in contexts.items()},
    }


@app.get("/v1/metadata")
def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera Builders"),
        "team_members": [x.strip() for x in os.getenv("TEAM_MEMBERS", "Anant").split(",") if x.strip()],
        "model": os.getenv("LLM_MODEL", "deterministic-rules-v1"),
        "approach": "stateful context store + deterministic trigger composer + optional LLM-free reply handler",
        "contact_email": os.getenv("CONTACT_EMAIL", ""),
        "version": APP_VERSION,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/v1/context")
def push_context(body: ContextBody):
    if body.scope not in contexts:
        return {"accepted": False, "reason": "invalid_scope", "details": body.scope}
    key = body.context_id
    current = contexts[body.scope].get(key)
    if current and current["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": current["version"]}
    contexts[body.scope][key] = {"version": body.version, "payload": body.payload, "delivered_at": body.delivered_at}
    return {"accepted": True, "ack_id": f"ack_{key}_v{body.version}", "stored_at": datetime.now(timezone.utc).isoformat()}


@app.post("/v1/tick")
def tick(body: TickBody):
    actions = []
    for trg_id in body.available_triggers:
        tentry = contexts["trigger"].get(trg_id)
        if not tentry:
            continue
        trigger = tentry["payload"]
        suppression = trigger.get("suppression_key", trg_id)
        if suppression in sent_suppressions:
            continue
        try:
            expires = trigger.get("expires_at")
            if expires and body.now > expires:
                continue
        except Exception:
            pass
        merchant_id = trigger.get("merchant_id") or trigger.get("payload", {}).get("merchant_id")
        if not merchant_id:
            continue
        mentry = contexts["merchant"].get(merchant_id)
        if not mentry:
            continue
        merchant = mentry["payload"]
        category_id = merchant.get("category_slug") or trigger.get("payload", {}).get("category")
        centry = contexts["category"].get(category_id)
        if not centry:
            continue
        category = centry["payload"]
        customer_id = trigger.get("customer_id") or trigger.get("payload", {}).get("customer_id")
        customer = contexts["customer"].get(customer_id, {}).get("payload") if customer_id else None
        result = compose(category, merchant, trigger, customer)
        if result.get("skip"):
            continue
        conversation_id = f"conv_{merchant_id}_{trg_id}_{uuid.uuid4().hex[:8]}"
        conversations[conversation_id] = {
            "merchant_id": merchant_id, "customer_id": customer_id,
            "active": True, "turns": [], "suppression_key": suppression,
        }
        sent_suppressions.add(suppression)
        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result["send_as"],
            "trigger_id": trg_id,
            "template_name": result.get("template_name", "vera_generic_v1"),
            "template_params": result.get("template_params", []),
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": result["suppression_key"],
            "rationale": result["rationale"],
        })
    return {"actions": actions}


@app.post("/v1/reply")
def reply(body: ReplyBody):
    conv = conversations.setdefault(body.conversation_id, {
        "merchant_id": body.merchant_id, "customer_id": body.customer_id,
        "active": True, "turns": [],
    })
    conv["turns"].append({"from": body.from_role, "message": body.message, "turn": body.turn_number})

    if _is_auto_reply(body.message):
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Detected likely canned WhatsApp auto-reply; avoid consuming conversation turns."}

    intent = _intent(body.message)
    if intent == "negative":
        conv["active"] = False
        return {"action": "end", "rationale": "Merchant/customer declined; gracefully ending the active conversation."}
    if intent == "wait":
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Recipient requested more time; backing off instead of pushing another message."}
    if intent == "positive":
        return {"action": "send", "body": "Absolutely — I’ll take that as a yes and move to the next step. If you want to change the plan, just tell me.", "cta": "open_ended", "rationale": "Recognised explicit positive intent and advanced without re-qualifying."}
    if intent == "question":
        return {"action": "send", "body": "Yes — I can help with that. Tell me which part you want to check first, and I’ll keep it specific to your business.", "cta": "open_ended", "rationale": "Recognised an information-seeking reply and kept the next step low-friction."}
    return {"action": "send", "body": "Got it. I’ll keep the next step focused on the specific trigger we just discussed.", "cta": "open_ended", "rationale": "Acknowledged the reply and preserved conversation continuity."}


@app.post("/v1/teardown")
def teardown():
    for v in contexts.values():
        v.clear()
    conversations.clear()
    sent_suppressions.clear()
    return {"ok": True, "state_cleared": True}
