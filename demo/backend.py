"""Mock backend services for the HomeSense voice-agent demo.

Stands in for the four external systems the agent talks to:

    customers.json   -> the account / device / order database (read-only)
    tickets.jsonl    -> the ticketing system (append-only)
    crm_log.jsonl    -> the CRM interaction log (append-only)
    handoffs.jsonl   -> the human-agent handoff queue (append-only)
    reports/*.json   -> end-of-session reports

Deliberately dependency-free and free of any LiveKit imports so it can be
unit-tested without credentials or a network. In production each function here
would be an HTTP call to a real system; the agent-side contract stays the same.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
CUSTOMERS_FILE = DATA_DIR / "customers.json"
TICKETS_FILE = DATA_DIR / "tickets.jsonl"
CRM_FILE = DATA_DIR / "crm_log.jsonl"
HANDOFFS_FILE = DATA_DIR / "handoffs.jsonl"
REPORTS_DIR = DATA_DIR / "reports"
EVENTS_FILE = DATA_DIR / "events.jsonl"
KB_DIR = DATA_DIR / "kb"
STATE_DIR = DATA_DIR / "state"

FIRST_TICKET_NUMBER = 1001

# Appends happen from tool calls on the session's event loop; a lock keeps the
# JSONL files from interleaving if two tools ever run concurrently.
_write_lock = threading.Lock()


class BackendError(Exception):
    """Raised when a lookup fails in a way the agent should tell the caller about."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class Customer:
    account_number: str
    name: str
    first_name: str
    # What the caller actually likes being called - "Prash" for Prashanth.
    # Falls back to first_name when the record does not set one.
    preferred_name: str
    phone: str
    email: str
    tier: str
    subscription: dict[str, Any]
    devices: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Customer:
        return cls(
            account_number=d["account_number"],
            name=d["name"],
            first_name=d.get("first_name") or d["name"].split()[0],
            preferred_name=(
                d.get("preferred_name") or d.get("first_name") or d["name"].split()[0]
            ),
            phone=d.get("phone", ""),
            email=d.get("email", ""),
            tier=d.get("tier", ""),
            subscription=d.get("subscription", {}),
            devices=d.get("devices", []),
            orders=d.get("orders", []),
            raw=d,
        )


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def normalize_phone(phone: str) -> str:
    """Reduce a phone number to digits, dropping a US country code.

    Lets '+1 (415) 555-0142', '4155550142' and '415-555-0142' all match - and
    also the spoken form STT produces, "four one five, five five five, zero one
    four two", which contains no digit characters at all.
    """
    digits = re.sub(r"\D", "", words_to_digits(phone or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


# Speech-to-text writes spoken digits as words: "one zero zero four two".
# Homophones are included because callers (and TTS) blur them over a phone line.
_NUMBER_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "nought": "0",
    "one": "1", "won": "1",
    "two": "2", "to": "2", "too": "2",
    "three": "3", "tree": "3",
    "four": "4", "for": "4", "fore": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8", "ate": "8",
    "nine": "9",
}


def words_to_digits(text: str) -> str:
    """Replace spoken number words with digits, leaving everything else alone."""
    return re.sub(
        r"\b(" + "|".join(_NUMBER_WORDS) + r")\b",
        lambda m: _NUMBER_WORDS[m.group(1).lower()],
        (text or "").lower(),
    )


def normalize_account(account: str) -> str:
    """Normalise an account number spoken over the phone.

    Handles 'h s 10042', 'HS 10042', 'hs-10042' and the spoken form STT
    actually produces, 'h s one zero zero four two'.
    """
    return re.sub(r"[^A-Z0-9]", "", words_to_digits(account).upper())


def account_digits(text: str) -> str:
    """Just the digit run from an account number, spoken or written."""
    return re.sub(r"\D", "", words_to_digits(text))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


# ---------------------------------------------------------------------------
# Customer database (read-only)
# ---------------------------------------------------------------------------


# The presenter's own mobile number, for the live phone beat. Kept in the
# environment so no real number is ever committed to customers.json.
PRESENTER_PHONE_PLACEHOLDER = "PRESENTER_PHONE_PLACEHOLDER"
# Unroutable default so the presenter account can never match a real caller
# when PRESENTER_PHONE is unset.
PRESENTER_PHONE_DEFAULT = "+10000000000"


def load_customers(path: Path | None = None) -> list[Customer]:
    """Load the account database, substituting the presenter's real number.

    The presenter account (HS-30001) ships with a placeholder phone. Setting
    PRESENTER_PHONE in .env.local swaps in the real cell at load time, so the
    live "call the agent from my phone" beat works without editing JSON.
    """
    src = path or CUSTOMERS_FILE
    with open(src, encoding="utf-8") as f:
        payload = json.load(f)

    presenter_phone = os.getenv("PRESENTER_PHONE", "").strip()
    customers = []
    for raw in payload["customers"]:
        if raw.get("phone") == PRESENTER_PHONE_PLACEHOLDER:
            # Without an override the placeholder can never match a real caller,
            # which is the safe default.
            raw = {**raw, "phone": presenter_phone or PRESENTER_PHONE_DEFAULT}
        customers.append(Customer.from_dict(raw))
    return customers


def lookup_by_phone(phone: str, path: Path | None = None) -> Customer | None:
    """Find a customer by caller ID. Returns None when the number is unknown."""
    target = normalize_phone(phone)
    if not target:
        return None
    for customer in load_customers(path):
        if normalize_phone(customer.phone) == target:
            return customer
    return None


# The numeric part of an account number must be at least this long before we
# will match on digits alone, so a stray number can't select an account.
MIN_ACCOUNT_DIGITS = 4


def lookup_by_account(account_number: str, path: Path | None = None) -> Customer | None:
    """Find a customer by account number, tolerant of how it arrives from STT.

    Matching happens in two passes:

      1. Exact, on the fully normalised string ('HS-10042' == 'hs 10042').
      2. On the digit run alone, allowing it to sit inside a longer utterance.

    Pass 2 matters in practice. Real STT renders a spoken account number as
    words and mishears letters: "H S 1 0 0 4 2" comes back as "eight s one zero
    zero four two". The letters are unreliable, the digits are not, and five
    digits identify an account unambiguously.
    """
    if not (account_number or "").strip():
        return None

    customers = load_customers(path)

    target = normalize_account(account_number)
    if target:
        for customer in customers:
            if normalize_account(customer.account_number) == target:
                return customer

    spoken_digits = account_digits(account_number)
    if len(spoken_digits) >= MIN_ACCOUNT_DIGITS:
        for customer in customers:
            digits = account_digits(customer.account_number)
            if len(digits) >= MIN_ACCOUNT_DIGITS and digits in spoken_digits:
                return customer

    return None


# ---------------------------------------------------------------------------
# Account queries
# ---------------------------------------------------------------------------


def get_recent_order(customer: Customer) -> dict[str, Any] | None:
    """Most recent order by placed_on date, or None if the account has no orders."""
    if not customer.orders:
        return None
    return max(customer.orders, key=lambda o: o.get("placed_on", ""))


# A match must clear this to count. It is set so that a stray shared word
# ("door" in "garage door opener") can never on its own resolve to a device the
# caller does not own - the agent must say "I don't see that" instead.
MIN_MATCH_SCORE = 2


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _device_score(device: dict[str, Any], query: str) -> tuple[int, int]:
    """Score how well a device matches a spoken description.

    Callers say 'my thermostat', 'the living room thermostat' or 'basement
    sensor' rather than a device ID. Scoring only credits *whole* concepts:

        device type present   ('thermostat', 'camera')      -> 3
        full room name present('living room', 'basement')   -> 2
        full device name present                            -> 5

    A partial word overlap earns nothing on its own and is returned separately
    as a tiebreak, so 'garage door opener' does not match 'Front Door Camera'
    just because both contain 'door'.
    """
    query_words = _tokens(query)
    if not query_words:
        return (0, 0)

    score = 0
    if (device.get("type") or "").lower() in query_words:
        score += 3

    room_words = _tokens(device.get("room", ""))
    if room_words and room_words <= query_words:
        score += 2

    name_words = _tokens(device.get("name", ""))
    if name_words and name_words <= query_words:
        score += 5

    haystack = _tokens(
        " ".join(str(device.get(k, "")) for k in ("name", "type", "room", "device_id"))
    )
    tiebreak = len(query_words & haystack)
    return (score, tiebreak)


def find_device(customer: Customer, device_name: str) -> dict[str, Any] | None:
    """Best fuzzy match for a spoken device name, or None if nothing matches."""
    scored = [(_device_score(d, device_name), d) for d in customer.devices]
    scored = [(s, d) for s, d in scored if s[0] >= MIN_MATCH_SCORE]
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def get_device_status(customer: Customer, device_name: str) -> dict[str, Any]:
    """Status payload for one device.

    Raises BackendError when the account has no such device, so the agent says
    'I don't see that on your account' instead of inventing one.
    """
    device = find_device(customer, device_name)
    if device is None:
        known = ", ".join(d["name"] for d in customer.devices) or "no devices"
        raise BackendError(
            f"No device matching '{device_name}' on account {customer.account_number}. "
            f"Registered devices: {known}."
        )
    return device


def get_temperature(customer: Customer, room: str) -> dict[str, Any]:
    """Current and target temperature for the thermostat in a room.

    Raises BackendError if the room has no temperature-reporting device.
    """
    candidates = [d for d in customer.devices if d.get("temperature_f") is not None]
    if not candidates:
        raise BackendError(
            f"Account {customer.account_number} has no temperature-reporting devices."
        )

    scored = [(_device_score(d, room), d) for d in candidates]
    scored = [(s, d) for s, d in scored if s[0] >= MIN_MATCH_SCORE]
    if not scored:
        rooms = ", ".join(sorted({d.get("room", "?") for d in candidates}))
        raise BackendError(
            f"No temperature reading for '{room}'. Rooms with a thermostat: {rooms}."
        )
    scored.sort(key=lambda pair: pair[0], reverse=True)
    device = scored[0][1]

    return {
        "device_id": device.get("device_id"),
        "device_name": device.get("name"),
        "room": device.get("room"),
        "status": device.get("status"),
        "temperature_f": device.get("temperature_f"),
        "target_temperature_f": device.get("target_temperature_f"),
        "mode": device.get("mode"),
        "last_seen": device.get("last_seen"),
    }


def get_subscription_status(customer: Customer) -> dict[str, Any]:
    return dict(customer.subscription)


def list_devices(customer: Customer, status: str | None = None) -> list[dict[str, Any]]:
    """Devices on the account, optionally only those with a given status."""
    devices = customer.devices
    if status is not None:
        devices = [d for d in devices if d["status"] == status]
    return [
        {
            "device_id": d.get("device_id"),
            "name": d.get("name"),
            "type": d.get("type"),
            "room": d.get("room"),
            "status": d.get("status"),
        }
        for d in devices
    ]


# ---------------------------------------------------------------------------
# Append-only writes: ticketing, CRM, handoffs
# ---------------------------------------------------------------------------


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _next_ticket_id(path: Path) -> str:
    """Sequential ticket IDs (TKT-1001, TKT-1002, ...) derived from the log.

    Rebuilding from the file rather than an in-memory counter keeps IDs stable
    across the agent restarts that happen between demo runs.
    """
    existing = read_jsonl(path)
    numbers = []
    for rec in existing:
        match = re.search(r"(\d+)$", rec.get("ticket_id", ""))
        if match:
            numbers.append(int(match.group(1)))
    nxt = max(numbers) + 1 if numbers else FIRST_TICKET_NUMBER
    return f"TKT-{nxt}"


def create_ticket(
    *,
    account_number: str,
    customer_name: str,
    issue_summary: str,
    device_id: str | None = None,
    priority: str = "normal",
    session_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """File a support ticket and return it (including the spoken ticket number)."""
    target = path or TICKETS_FILE
    with _write_lock:
        ticket_id = _next_ticket_id(target)

    ticket = {
        "ticket_id": ticket_id,
        "created_at": _now_iso(),
        "account_number": account_number,
        "customer_name": customer_name,
        "issue_summary": issue_summary,
        "device_id": device_id,
        "priority": priority,
        "status": "open",
        "channel": "voice-agent",
        "session_id": session_id,
    }
    _append_jsonl(target, ticket)
    return ticket


def emit_event(
    kind: str,
    *,
    session_id: str | None = None,
    room: str | None = None,
    path: Path | None = None,
    **fields: Any,
) -> dict[str, Any] | None:
    """Append one event to the live activity stream, for the /flow and /stats UIs.

    Deliberately separate from crm_log: the CRM is the business record, this is
    telemetry for the visualisers. Best-effort by contract - instrumentation
    must never be able to break a live call, so every failure is swallowed and
    reported as None.
    """
    try:
        record = {
            "ts": time.time(),
            "at": _now_iso(),
            "kind": kind,
            "session_id": session_id,
            "room": room,
            **fields,
        }
        _append_jsonl(path or EVENTS_FILE, record)
        return record
    except Exception:  # noqa: BLE001 - telemetry must never raise
        return None


def read_events(
    since: float | None = None, limit: int = 200, path: Path | None = None
) -> list[dict[str, Any]]:
    """Events newer than `since` (a unix timestamp), oldest first."""
    events = read_jsonl(path or EVENTS_FILE)
    if since is not None:
        events = [e for e in events if float(e.get("ts") or 0) > since]
    return events[-limit:]


def crm_log(event: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Append one interaction event to the CRM log.

    Called for every tool invocation so the CRM row is built incrementally
    rather than only at the end of the call — if the call drops mid-way, the
    partial history survives.
    """
    record = {"logged_at": _now_iso(), **event}
    _append_jsonl(path or CRM_FILE, record)
    return record


def record_handoff(
    *,
    account_number: str | None,
    customer_name: str | None,
    summary: str,
    reason: str,
    mode: str,
    session_id: str | None = None,
    ticket_id: str | None = None,
    transcript: list[dict[str, Any]] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Queue a warm handoff for a human agent, including the context summary."""
    handoff = {
        "handoff_id": f"HO-{uuid.uuid4().hex[:8].upper()}",
        "created_at": _now_iso(),
        "account_number": account_number,
        "customer_name": customer_name,
        "reason": reason,
        "summary": summary,
        "mode": mode,
        "ticket_id": ticket_id,
        "session_id": session_id,
        "transcript": transcript or [],
    }
    _append_jsonl(path or HANDOFFS_FILE, handoff)
    return handoff


def add_account_note(
    *,
    account_number: str,
    customer_name: str,
    note: str,
    session_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append a free-text note to the account's CRM record."""
    record = {
        "note_id": f"NOTE-{uuid.uuid4().hex[:8].upper()}",
        "session_id": session_id,
        "event_type": "account_note",
        "account_number": account_number,
        "customer_name": customer_name,
        "note": note,
    }
    return crm_log(record, path=path)


def get_account_notes(
    account_number: str, path: Path | None = None
) -> list[dict[str, Any]]:
    """Every note previously left on an account."""
    return [
        r
        for r in read_jsonl(path or CRM_FILE)
        if r.get("event_type") == "account_note"
        and r.get("account_number") == account_number
    ]


# ---------------------------------------------------------------------------
# Knowledge base (how-to and policy answers)
# ---------------------------------------------------------------------------

# Words too common in support prose to discriminate between articles.
_KB_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "your", "you", "are", "not",
    "has", "have", "was", "were", "can", "will", "how", "what", "why", "does",
    "did", "from", "out", "off", "its", "it's", "but", "all", "any", "one",
    "two", "get", "got", "his", "her", "our", "who", "when", "where", "them",
    "they", "there", "then", "than", "some", "more", "most", "very", "just",
    "about", "after", "before", "into", "over", "under", "again", "still",
    "homesense", "device", "devices", "please", "help", "need", "want",
}


def _stem(word: str) -> str:
    """Crude suffix stripping so 'return' matches 'returns' and 'returned'.

    Not linguistically correct, and it does not need to be - it only has to make
    the obvious spoken variants collide on a six-article corpus.
    """
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            stem = word[: -len(suffix)]
            if suffix == "ies":
                return stem + "y"
            return stem
    return word


def _kb_tokens(text: str) -> list[str]:
    return [
        _stem(w)
        for w in re.findall(r"[a-z0-9']+", (text or "").lower())
        if len(w) > 2 and w not in _KB_STOPWORDS
    ]


def _kb_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def load_kb(kb_dir: Path | None = None) -> list[dict[str, Any]]:
    """Read every knowledge-base article off disk."""
    src = kb_dir or KB_DIR
    if not src.exists():
        return []
    articles = []
    for path in sorted(src.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        articles.append(
            {
                "slug": path.stem,
                "title": _kb_title(text, path.stem.replace("-", " ").title()),
                "body": text,
                "tokens": _kb_tokens(text),
            }
        )
    return articles


def _snippet(body: str, query_tokens: set[str], max_chars: int = 700) -> str:
    """Pull the passages that best answer the query, flattened for speech.

    Returns the top two matching paragraphs in document order rather than just
    the single best one: the highest-scoring paragraph is often the article's
    intro ("a reset clears most faults") while the paragraph the caller actually
    needs is the one after it with the steps in.
    """
    paragraphs = [
        p.strip() for p in body.split("\n\n") if p.strip() and not p.startswith("# ")
    ]
    if not paragraphs:
        return re.sub(r"\s+", " ", body)[:max_chars].strip()

    scored = []
    for index, para in enumerate(paragraphs):
        tokens = _kb_tokens(para)
        distinct = len(set(tokens) & query_tokens)
        total = sum(1 for t in tokens if t in query_tokens)
        scored.append((distinct, total, -index, index, para))

    scored.sort(reverse=True)
    chosen = sorted(scored[:2], key=lambda s: s[3])
    text = " ".join(re.sub(r"[*#`]", "", para) for *_, para in chosen)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= max_chars else text[:max_chars].rsplit(" ", 1)[0] + "…"


def search_kb(
    query: str, *, limit: int = 2, kb_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Rank knowledge-base articles against a spoken question.

    Deliberately a small TF-style keyword score rather than embeddings: it needs
    no model, no network and no index build, and on a corpus this size it is
    both accurate and instant. In production this is the seam where a real
    vector store or a managed RAG service would slot in - the tool contract the
    agent sees would not change.
    """
    query_tokens = set(_kb_tokens(query))
    if not query_tokens:
        return []

    results = []
    for article in load_kb(kb_dir):
        tokens = article["tokens"]
        if not tokens:
            continue
        counts = {t: tokens.count(t) for t in query_tokens if t in tokens}
        if not counts:
            continue
        # Distinct term matches dominate; repetition is a weak tiebreak. Title
        # hits count double because titles name the topic.
        title_tokens = set(_kb_tokens(article["title"]))
        score = (
            len(counts) * 10
            + sum(counts.values())
            + len(query_tokens & title_tokens) * 10
        )
        results.append(
            {
                "title": article["title"],
                "slug": article["slug"],
                "score": score,
                "snippet": _snippet(article["body"], query_tokens),
            }
        )

    results.sort(key=lambda r: (-r["score"], r["slug"]))
    return results[:limit]


# ---------------------------------------------------------------------------
# Crash-resilience checkpoints
# ---------------------------------------------------------------------------


def checkpoint_path(room_name: str, state_dir: Path | None = None) -> Path:
    return (state_dir or STATE_DIR) / f"{_slug(room_name) or 'unknown'}.json"


# A room name can be reused, so a checkpoint is only trusted while the call it
# belongs to could plausibly still be in progress.
CHECKPOINT_MAX_AGE_SECONDS = 600  # 10 minutes


def save_checkpoint(
    *,
    room_name: str,
    account_number: str | None,
    customer_name: str | None,
    phase: str,
    chat_items: list[dict[str, Any]],
    tickets: list[str] | None = None,
    job_id: str | None = None,
    participant_identity: str | None = None,
    max_items: int = 20,
    state_dir: Path | None = None,
) -> Path:
    """Persist enough state to resume this call if the agent process dies.

    File-per-room here; in production this is Redis keyed by call ID with a TTL.
    `job_id` and `participant_identity` are recorded so a resume can be checked
    against who is actually on the line rather than trusting the room name alone.
    """
    path = checkpoint_path(room_name, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "room": room_name,
        "updated_at": _now_iso(),
        "updated_ts": time.time(),
        "job_id": job_id,
        "participant_identity": participant_identity,
        "account_number": account_number,
        "customer_name": customer_name,
        "phase": phase,
        "tickets": tickets or [],
        "chat_items": chat_items[-max_items:],
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    # Atomic swap so a crash mid-write cannot leave a truncated checkpoint.
    tmp.replace(path)
    return path


def checkpoint_age_seconds(snapshot: dict[str, Any]) -> float:
    """Seconds since the snapshot was written; +inf if it carries no timestamp."""
    ts = snapshot.get("updated_ts")
    if not isinstance(ts, (int, float)):
        return float("inf")
    return max(0.0, time.time() - ts)


def load_checkpoint(
    room_name: str,
    state_dir: Path | None = None,
    *,
    max_age_seconds: float | None = CHECKPOINT_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    """Load a checkpoint, ignoring one too old to belong to the live call.

    Without the age check a recycled room name could resurrect a stranger's
    conversation into a new call - the worst possible failure for this demo.
    """
    path = checkpoint_path(room_name, state_dir)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            snapshot = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(snapshot, dict):
        return None

    if max_age_seconds is not None:
        age = checkpoint_age_seconds(snapshot)
        if age > max_age_seconds:
            return None
    return snapshot


def clear_checkpoint(room_name: str, state_dir: Path | None = None) -> None:
    checkpoint_path(room_name, state_dir).unlink(missing_ok=True)


def save_session_report(
    report: dict[str, Any],
    *,
    session_id: str | None = None,
    reports_dir: Path | None = None,
) -> Path:
    """Write the end-of-session report and return the path written."""
    target_dir = reports_dir or REPORTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    name = session_id or report.get("session_id") or f"session-{int(time.time())}"
    path = target_dir / f"{_slug(str(name))}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return path


# ---------------------------------------------------------------------------
# Spoken-form helpers (keep number formatting out of the LLM's hands)
# ---------------------------------------------------------------------------


def spoken_order_status(order: dict[str, Any]) -> str:
    """A one-line, speakable summary of an order."""
    items = ", ".join(
        f"{i.get('quantity', 1)} {i.get('description', 'item')}" for i in order.get("items", [])
    )
    status = order.get("status", "unknown")
    if status == "shipped":
        return (
            f"Order {order['order_id']} ({items}) shipped via {order.get('carrier', 'carrier')}, "
            f"arriving about {order.get('eta_days', '?')} days from now "
            f"on {order.get('estimated_delivery', 'an unconfirmed date')}."
        )
    if status == "delivered":
        return (
            f"Order {order['order_id']} ({items}) was delivered on "
            f"{order.get('delivered_on', 'an earlier date')}."
        )
    return f"Order {order['order_id']} ({items}) is currently {status}."
