"""Unit tests for the mock backend. No credentials or network required."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import backend
from backend import BackendError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def prash():
    """HS-10042 - the presenter's account, and the main demo dataset."""
    return backend.lookup_by_account("HS-10042")


@pytest.fixture
def priya():
    """HS-30001 - the hallway/garage dataset, reachable on a fake number."""
    return backend.lookup_by_account("HS-30001")


@pytest.fixture
def daniel():
    return backend.lookup_by_account("HS-20077")


@pytest.fixture
def tmp_tickets(tmp_path: Path) -> Path:
    return tmp_path / "tickets.jsonl"


@pytest.fixture
def tmp_crm(tmp_path: Path) -> Path:
    return tmp_path / "crm_log.jsonl"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+14155550142", "4155550142"),
        ("14155550142", "4155550142"),
        ("4155550142", "4155550142"),
        ("+1 (415) 555-0142", "4155550142"),
        ("415-555-0142", "4155550142"),
        ("", ""),
    ],
)
def test_normalize_phone(raw, expected):
    assert backend.normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("HS-10042", "HS10042"),
        ("hs 10042", "HS10042"),
        ("H S 1 0 0 4 2", "HS10042"),
        ("hs-10042 ", "HS10042"),
    ],
)
def test_normalize_account(raw, expected):
    assert backend.normalize_account(raw) == expected


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_load_customers_has_three():
    customers = backend.load_customers()
    assert len(customers) == 3
    assert {c.account_number for c in customers} == {"HS-10042", "HS-20077", "HS-30001"}


# ---------------------------------------------------------------------------
# Presenter account (HS-30001) and the PRESENTER_PHONE override
# ---------------------------------------------------------------------------


def test_presenter_phone_default_when_env_unset(monkeypatch):
    """With no override the account falls back to an unroutable number."""
    monkeypatch.delenv("PRESENTER_PHONE", raising=False)
    presenter = backend.lookup_by_account("HS-10042")
    assert presenter.name == "Prashanth Ganapathy"
    assert presenter.preferred_name == "Prash"
    assert presenter.phone == backend.PRESENTER_PHONE_DEFAULT == "+10000000000"


def test_presenter_default_cannot_collide_with_a_real_caller(monkeypatch):
    """No other seeded account shares the fallback number."""
    monkeypatch.delenv("PRESENTER_PHONE", raising=False)
    default = backend.normalize_phone(backend.PRESENTER_PHONE_DEFAULT)
    others = [
        backend.normalize_phone(c.phone)
        for c in backend.load_customers()
        if c.account_number != "HS-10042"
    ]
    assert default not in others
    # The JSON on disk still carries only the placeholder, never a real number.
    raw = json.loads((backend.CUSTOMERS_FILE).read_text())
    presenter_raw = next(c for c in raw["customers"] if c["account_number"] == "HS-10042")
    assert presenter_raw["phone"] == backend.PRESENTER_PHONE_PLACEHOLDER


def test_presenter_phone_override_applies(monkeypatch):
    monkeypatch.setenv("PRESENTER_PHONE", "+12065551234")
    presenter = backend.lookup_by_account("HS-10042")
    assert presenter.phone == "+12065551234"

    found = backend.lookup_by_phone("+1 (206) 555-1234")
    assert found is not None
    assert found.account_number == "HS-10042"
    assert found.preferred_name == "Prash"


def test_presenter_override_does_not_disturb_other_customers(monkeypatch):
    monkeypatch.setenv("PRESENTER_PHONE", "+12065551234")
    assert backend.lookup_by_phone("+14155550142").account_number == "HS-30001"
    assert backend.lookup_by_phone("+14155550177").account_number == "HS-20077"


def test_presenter_account_answers_every_demo_question(monkeypatch):
    """The phone beat must survive the same questions as the web beat."""
    monkeypatch.setenv("PRESENTER_PHONE", "+12065551234")
    p = backend.lookup_by_phone("+12065551234")
    assert p.account_number == "HS-10042"
    assert backend.get_recent_order(p)["order_id"] == "HS-ORD-88213"
    assert backend.get_device_status(p, "thermostat")["status"] == "active"
    assert backend.get_temperature(p, "living room")["temperature_f"] == 71
    assert backend.get_device_status(p, "basement sensor")["status"] == "offline"
    assert backend.get_subscription_status(p)["status"] == "active"


def test_second_account_answers_every_demo_question(priya):
    """HS-30001 keeps the hallway/garage dataset for a contrasting caller."""
    assert backend.get_recent_order(priya)["order_id"] == "HS-ORD-91455"
    assert backend.get_temperature(priya, "hallway")["temperature_f"] == 72
    assert backend.get_device_status(priya, "garage sensor")["status"] == "offline"


def test_lookup_by_phone_finds_priya():
    customer = backend.lookup_by_phone("+14155550142")
    assert customer is not None
    assert customer.name == "Priya Sharma"
    assert customer.first_name == "Priya"


def test_lookup_by_phone_tolerates_formatting():
    assert backend.lookup_by_phone("(415) 555-0142").account_number == "HS-30001"
    assert backend.lookup_by_phone("4155550177").account_number == "HS-20077"


def test_lookup_by_phone_unknown_returns_none():
    assert backend.lookup_by_phone("+15005550000") is None
    assert backend.lookup_by_phone("") is None


def test_lookup_by_account_tolerates_spoken_form():
    """STT renders a spoken account number with spaces; it must still match."""
    for spoken in ["HS-10042", "hs10042", "H S 1 0 0 4 2", "hs 10042"]:
        customer = backend.lookup_by_account(spoken)
        assert customer is not None, spoken
        assert customer.account_number == "HS-10042"


def test_lookup_by_account_unknown_returns_none():
    assert backend.lookup_by_account("HS-99999") is None
    assert backend.lookup_by_account("") is None
    assert backend.lookup_by_account("   ") is None


@pytest.mark.parametrize(
    "heard",
    [
        # Verbatim output observed from deepgram/nova-3 for "H S 1 0 0 4 2":
        # digits arrive as words and the 'H' is misheard as 'eight'.
        "My account number is eight s one zero zero four two",
        "h s one zero zero four two",
        "H S one oh oh four two",
        "it's hs one zero zero four two",
        "one zero zero four two",
        "10042",
        "my account is HS 10042",
    ],
)
def test_lookup_by_account_survives_real_stt_output(heard):
    """STT writes spoken numbers as words and mishears letters; digits carry it."""
    customer = backend.lookup_by_account(heard)
    assert customer is not None, heard
    assert customer.account_number == "HS-10042"


def test_lookup_by_account_spoken_form_picks_right_customer():
    assert backend.lookup_by_account("h s two zero zero seven seven").account_number == "HS-20077"


def test_words_to_digits():
    assert backend.words_to_digits("one zero zero four two") == "1 0 0 4 2"
    assert backend.words_to_digits("eight s one") == "8 s 1"
    assert backend.words_to_digits("no numbers here") == "no numbers here"


def test_short_digit_runs_do_not_match():
    """A stray number in conversation must not select an account."""
    assert backend.lookup_by_account("about two") is None
    assert backend.lookup_by_account("100") is None


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def test_get_recent_order_picks_newest(prash):
    order = backend.get_recent_order(prash)
    assert order["order_id"] == "HS-ORD-88213"
    assert order["status"] == "shipped"
    assert order["carrier"] == "UPS"
    assert order["eta_days"] == 2


def test_recent_order_for_daniel_is_delivered(daniel):
    order = backend.get_recent_order(daniel)
    assert order["order_id"] == "HS-ORD-90112"
    assert order["status"] == "delivered"


def test_spoken_order_status_shipped(prash):
    text = backend.spoken_order_status(backend.get_recent_order(prash))
    assert "HS-ORD-88213" in text
    assert "UPS" in text
    assert "Window Sensor" in text


def test_spoken_order_status_delivered(daniel):
    text = backend.spoken_order_status(backend.get_recent_order(daniel))
    assert "delivered on" in text


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spoken,expected_id",
    [
        ("thermostat", "THERM-LR-01"),
        ("living room thermostat", "THERM-LR-01"),
        ("front door camera", "CAM-FD-01"),
        ("front door lock", "LOCK-FD-01"),
        ("basement sensor", "SENS-BSMT-01"),
        ("Basement Water Sensor", "SENS-BSMT-01"),
    ],
)
def test_get_device_status_fuzzy_match(prash, spoken, expected_id):
    device = backend.get_device_status(prash, spoken)
    assert device["device_id"] == expected_id


def test_thermostat_is_active(prash):
    device = backend.get_device_status(prash, "thermostat")
    assert device["status"] == "active"


def test_basement_sensor_is_offline(prash):
    """The deliberate 'cannot be resolved on the call' hook."""
    device = backend.get_device_status(prash, "basement sensor")
    assert device["status"] == "offline"
    assert device["note"]


def test_daniel_thermostat_is_inactive(daniel):
    device = backend.get_device_status(daniel, "thermostat")
    assert device["status"] == "inactive"


@pytest.mark.parametrize(
    "spoken",
    [
        "garage door opener",  # shares the word 'door' with two real devices
        "hot tub",
        "smoke detector",
        "solar panel",
    ],
)
def test_get_device_status_unknown_raises(prash, spoken):
    """A device the caller does not own must never resolve to a different one."""
    with pytest.raises(BackendError) as exc:
        backend.get_device_status(prash, spoken)
    assert "No device matching" in str(exc.value)
    # The error lists what they do have, so the agent can offer an alternative.
    assert "Living Room Thermostat" in str(exc.value)


def test_bare_device_type_resolves(prash):
    """Callers often say just 'camera' or 'lock'."""
    assert backend.get_device_status(prash, "camera")["device_id"] == "CAM-FD-01"
    assert backend.get_device_status(prash, "lock")["device_id"] == "LOCK-FD-01"
    assert backend.get_device_status(prash, "sensor")["device_id"] == "SENS-BSMT-01"


def test_conversational_phrasing_resolves(prash):
    """The LLM passes through the caller's wording, filler words and all."""
    device = backend.get_device_status(prash, "the thermostat in the living room")
    assert device["device_id"] == "THERM-LR-01"


def test_list_devices(prash):
    devices = backend.list_devices(prash)
    assert len(devices) == 4
    assert {d["status"] for d in devices} == {"active", "online", "locked", "offline"}


# ---------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------


def test_get_temperature_living_room(prash):
    reading = backend.get_temperature(prash, "living room")
    assert reading["temperature_f"] == 71
    assert reading["target_temperature_f"] == 70
    assert reading["room"] == "Living Room"
    assert reading["status"] == "active"


def test_get_temperature_via_device_wording(prash):
    """'my thermostat' should read a temperature just as well as a room name."""
    assert backend.get_temperature(prash, "thermostat")["temperature_f"] == 71


def test_get_temperature_single_word_room(daniel):
    assert backend.get_temperature(daniel, "bedroom")["temperature_f"] == 68


@pytest.mark.parametrize("room", ["attic", "garage", "kitchen"])
def test_get_temperature_unknown_room_raises(prash, room):
    with pytest.raises(BackendError) as exc:
        backend.get_temperature(prash, room)
    assert "No temperature reading" in str(exc.value)


def test_get_temperature_raises_when_no_thermostats(prash):
    """An account with no temperature-reporting device gets a clear error."""
    stripped = backend.Customer.from_dict({**prash.raw, "devices": []})
    with pytest.raises(BackendError) as exc:
        backend.get_temperature(stripped, "living room")
    assert "no temperature-reporting devices" in str(exc.value)


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------


def test_subscription_active(prash):
    sub = backend.get_subscription_status(prash)
    assert sub["status"] == "active"
    assert sub["renews_on"] == "2026-09-01"


def test_subscription_lapsed(daniel):
    assert backend.get_subscription_status(daniel)["status"] == "lapsed"


# ---------------------------------------------------------------------------
# Ticketing
# ---------------------------------------------------------------------------


def test_create_ticket_returns_id_and_persists(tmp_tickets, prash):
    ticket = backend.create_ticket(
        account_number=prash.account_number,
        customer_name=prash.name,
        issue_summary="Basement water sensor offline for 8 days.",
        device_id="SENS-BSMT-01",
        session_id="test-session",
        path=tmp_tickets,
    )

    assert ticket["ticket_id"] == "TKT-1001"
    assert ticket["status"] == "open"
    assert ticket["device_id"] == "SENS-BSMT-01"

    rows = backend.read_jsonl(tmp_tickets)
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == "TKT-1001"
    assert rows[0]["account_number"] == "HS-10042"


def test_ticket_ids_increment(tmp_tickets, prash):
    ids = [
        backend.create_ticket(
            account_number=prash.account_number,
            customer_name=prash.name,
            issue_summary=f"issue {i}",
            path=tmp_tickets,
        )["ticket_id"]
        for i in range(3)
    ]
    assert ids == ["TKT-1001", "TKT-1002", "TKT-1003"]


def test_ticket_ids_resume_after_restart(tmp_tickets, prash):
    """IDs come from the file, so a restarted agent does not reuse numbers."""
    backend.create_ticket(
        account_number=prash.account_number,
        customer_name=prash.name,
        issue_summary="first",
        path=tmp_tickets,
    )
    # Simulate a fresh process: nothing cached, read the file again.
    second = backend.create_ticket(
        account_number=prash.account_number,
        customer_name=prash.name,
        issue_summary="second",
        path=tmp_tickets,
    )
    assert second["ticket_id"] == "TKT-1002"


# ---------------------------------------------------------------------------
# CRM log
# ---------------------------------------------------------------------------


def test_crm_log_appends(tmp_crm):
    backend.crm_log({"session_id": "s1", "event_type": "call_started"}, path=tmp_crm)
    backend.crm_log({"session_id": "s1", "event_type": "tool_call", "tool": "x"}, path=tmp_crm)

    rows = backend.read_jsonl(tmp_crm)
    assert len(rows) == 2
    assert rows[0]["event_type"] == "call_started"
    assert rows[1]["tool"] == "x"
    assert all("logged_at" in r for r in rows)


def test_crm_log_returns_record(tmp_crm):
    record = backend.crm_log({"event_type": "test"}, path=tmp_crm)
    assert record["event_type"] == "test"
    assert "logged_at" in record


def test_read_jsonl_missing_file_is_empty(tmp_path):
    assert backend.read_jsonl(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# Handoffs and reports
# ---------------------------------------------------------------------------


def test_record_handoff(tmp_path, prash):
    path = tmp_path / "handoffs.jsonl"
    handoff = backend.record_handoff(
        account_number=prash.account_number,
        customer_name=prash.name,
        summary="Caller's basement sensor is offline; ticket TKT-1001 filed.",
        reason="caller asked for a human",
        mode="simulated_console",
        session_id="s1",
        ticket_id="TKT-1001",
        transcript=[{"role": "user", "text": "hi"}],
        path=path,
    )

    assert handoff["handoff_id"].startswith("HO-")
    assert handoff["ticket_id"] == "TKT-1001"

    rows = backend.read_jsonl(path)
    assert len(rows) == 1
    assert rows[0]["summary"].startswith("Caller's basement sensor")
    assert rows[0]["transcript"] == [{"role": "user", "text": "hi"}]


def test_save_session_report(tmp_path):
    path = backend.save_session_report(
        {"session_id": "AJ_abc123", "tickets_created": ["TKT-1001"]},
        session_id="AJ_abc123",
        reports_dir=tmp_path,
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["session_id"] == "AJ_abc123"
    assert data["tickets_created"] == ["TKT-1001"]


# ---------------------------------------------------------------------------
# Demo-script guardrails: the exact facts the presenter will say out loud
# ---------------------------------------------------------------------------


def test_demo_script_facts_hold(monkeypatch):
    """The exact facts the presenter will say out loud, on HS-10042."""
    monkeypatch.setenv("PRESENTER_PHONE", "+12065551234")
    prash = backend.lookup_by_phone("+12065551234")
    assert prash.name == "Prashanth Ganapathy"
    assert prash.preferred_name == "Prash"
    assert prash.account_number == "HS-10042"

    order = backend.get_recent_order(prash)
    assert (order["order_id"], order["status"], order["carrier"]) == (
        "HS-ORD-88213",
        "shipped",
        "UPS",
    )

    assert backend.get_device_status(prash, "thermostat")["status"] == "active"
    assert backend.get_temperature(prash, "living room")["temperature_f"] == 71
    assert backend.get_device_status(prash, "basement sensor")["status"] == "offline"
    assert backend.get_subscription_status(prash)["status"] == "active"


# ---------------------------------------------------------------------------
# Knowledge base (item 10)
# ---------------------------------------------------------------------------


def test_kb_articles_all_load():
    articles = backend.load_kb()
    assert len(articles) >= 6
    assert all(a["title"] and a["body"] and a["tokens"] for a in articles)
    # Titles come from the markdown H1, not the filename.
    assert "Sensor offline" in {a["title"] for a in articles}


@pytest.mark.parametrize(
    "query,expected_title",
    [
        ("how do I reset my thermostat", "Resetting a HomeSense thermostat"),
        ("my camera keeps going offline", "Camera showing offline"),
        ("the door lock battery is dead", "Door lock battery"),
        ("my sensor stopped reporting", "Sensor offline"),
        ("how much does the plan cost", "Premium Cloud Video plan"),
        ("what does the subscription include", "Premium Cloud Video plan"),
        ("can I return this", "Returns and warranty"),
        ("how long is the warranty", "Returns and warranty"),
    ],
)
def test_search_kb_ranks_the_right_article_first(query, expected_title):
    results = backend.search_kb(query)
    assert results, f"no KB match for {query!r}"
    assert results[0]["title"] == expected_title


def test_search_kb_returns_at_most_two():
    assert len(backend.search_kb("offline camera sensor thermostat plan")) <= 2


def test_search_kb_empty_for_nonsense():
    assert backend.search_kb("xyzzy plugh frobnicate") == []
    assert backend.search_kb("") == []


def test_search_kb_snippet_carries_the_answer():
    """The snippet must contain the actual policy, not just the intro."""
    result = backend.search_kb("my sensor is still offline after a power cycle")[0]
    assert result["title"] == "Sensor offline"
    assert "ticket" in result["snippet"].lower()


def test_search_kb_snippet_is_speakable():
    """No markdown syntax should survive into something the agent reads out."""
    for result in backend.search_kb("how do I reset my thermostat"):
        assert "**" not in result["snippet"]
        assert "\n" not in result["snippet"]


def test_stemming_matches_spoken_variants():
    assert backend._stem("returns") == backend._stem("return")
    assert backend._stem("returned") == backend._stem("return")
    assert backend._stem("sensors") == backend._stem("sensor")


# ---------------------------------------------------------------------------
# Account notes (item 12a)
# ---------------------------------------------------------------------------


def test_add_account_note_persists(tmp_crm, prash):
    record = backend.add_account_note(
        account_number=prash.account_number,
        customer_name=prash.name,
        note="Prefers morning appointments.",
        session_id="s1",
        path=tmp_crm,
    )
    assert record["note_id"].startswith("NOTE-")
    assert record["event_type"] == "account_note"

    notes = backend.get_account_notes(prash.account_number, path=tmp_crm)
    assert len(notes) == 1
    assert notes[0]["note"] == "Prefers morning appointments."


def test_account_notes_are_scoped_to_one_account(tmp_crm, prash, daniel):
    backend.add_account_note(
        account_number=prash.account_number, customer_name=prash.name,
        note="Priya note", path=tmp_crm,
    )
    backend.add_account_note(
        account_number=daniel.account_number, customer_name=daniel.name,
        note="Daniel note", path=tmp_crm,
    )
    assert [n["note"] for n in backend.get_account_notes("HS-10042", path=tmp_crm)] == ["Priya note"]
    assert [n["note"] for n in backend.get_account_notes("HS-20077", path=tmp_crm)] == ["Daniel note"]


# ---------------------------------------------------------------------------
# Checkpoints (item 9)
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_checkpoint_round_trip(state_dir):
    backend.save_checkpoint(
        room_name="call-abc",
        account_number="HS-10042",
        customer_name="Priya Sharma",
        phase="support",
        chat_items=[{"role": "user", "text": "my sensor is offline"}],
        tickets=["TKT-1001"],
        state_dir=state_dir,
    )
    snap = backend.load_checkpoint("call-abc", state_dir=state_dir)
    assert snap["account_number"] == "HS-10042"
    assert snap["phase"] == "support"
    assert snap["tickets"] == ["TKT-1001"]
    assert snap["chat_items"][0]["text"] == "my sensor is offline"


def test_checkpoint_missing_returns_none(state_dir):
    assert backend.load_checkpoint("never-existed", state_dir=state_dir) is None


def test_checkpoint_truncates_history(state_dir):
    items = [{"role": "user", "text": f"turn {i}"} for i in range(50)]
    backend.save_checkpoint(
        room_name="r", account_number="HS-10042", customer_name="P",
        phase="support", chat_items=items, max_items=5, state_dir=state_dir,
    )
    snap = backend.load_checkpoint("r", state_dir=state_dir)
    assert len(snap["chat_items"]) == 5
    assert snap["chat_items"][-1]["text"] == "turn 49"


def test_checkpoint_overwrites_not_appends(state_dir):
    for phase in ("identify", "support"):
        backend.save_checkpoint(
            room_name="r", account_number="HS-10042", customer_name="P",
            phase=phase, chat_items=[], state_dir=state_dir,
        )
    assert backend.load_checkpoint("r", state_dir=state_dir)["phase"] == "support"
    assert len(list(state_dir.glob("*.json"))) == 1


def test_checkpoint_leaves_no_temp_file(state_dir):
    """The write is atomic, so no .tmp should survive."""
    backend.save_checkpoint(
        room_name="r", account_number="HS-10042", customer_name="P",
        phase="support", chat_items=[], state_dir=state_dir,
    )
    assert list(state_dir.glob("*.tmp")) == []


def test_corrupt_checkpoint_is_ignored(state_dir):
    """A half-written file must not crash the resume path."""
    state_dir.mkdir(parents=True, exist_ok=True)
    backend.checkpoint_path("r", state_dir).write_text("{not json")
    assert backend.load_checkpoint("r", state_dir=state_dir) is None


def test_clear_checkpoint(state_dir):
    backend.save_checkpoint(
        room_name="r", account_number="HS-10042", customer_name="P",
        phase="support", chat_items=[], state_dir=state_dir,
    )
    backend.clear_checkpoint("r", state_dir=state_dir)
    assert backend.load_checkpoint("r", state_dir=state_dir) is None
    # Clearing twice is not an error.
    backend.clear_checkpoint("r", state_dir=state_dir)


# ---------------------------------------------------------------------------
# Checkpoint expiry and identity (crash resilience hardening)
# ---------------------------------------------------------------------------


def test_checkpoint_records_job_and_participant(state_dir):
    backend.save_checkpoint(
        room_name="call-abc", account_number="HS-10042", customer_name="Priya Sharma",
        phase="support", chat_items=[], job_id="AJ_xyz",
        participant_identity="sip_+14155550142", state_dir=state_dir,
    )
    snap = backend.load_checkpoint("call-abc", state_dir=state_dir)
    assert snap["job_id"] == "AJ_xyz"
    assert snap["participant_identity"] == "sip_+14155550142"


def test_fresh_checkpoint_is_returned(state_dir):
    backend.save_checkpoint(
        room_name="r", account_number="HS-10042", customer_name="P",
        phase="support", chat_items=[], state_dir=state_dir,
    )
    assert backend.load_checkpoint("r", state_dir=state_dir) is not None
    assert backend.checkpoint_age_seconds(
        backend.load_checkpoint("r", state_dir=state_dir)
    ) < 5


def test_stale_checkpoint_is_ignored(state_dir):
    """A recycled room name must not resurrect a stranger's conversation."""
    import json as _json
    import time as _time

    backend.save_checkpoint(
        room_name="r", account_number="HS-10042", customer_name="Priya Sharma",
        phase="support", chat_items=[{"role": "user", "text": "secret"}],
        state_dir=state_dir,
    )
    path = backend.checkpoint_path("r", state_dir)
    payload = _json.loads(path.read_text())
    payload["updated_ts"] = _time.time() - (backend.CHECKPOINT_MAX_AGE_SECONDS + 60)
    path.write_text(_json.dumps(payload))

    assert backend.load_checkpoint("r", state_dir=state_dir) is None
    # The file is still there — it is ignored, not silently deleted.
    assert path.exists()


def test_checkpoint_just_inside_the_window_is_kept(state_dir):
    import json as _json
    import time as _time

    backend.save_checkpoint(
        room_name="r", account_number="HS-10042", customer_name="P",
        phase="support", chat_items=[], state_dir=state_dir,
    )
    path = backend.checkpoint_path("r", state_dir)
    payload = _json.loads(path.read_text())
    payload["updated_ts"] = _time.time() - (backend.CHECKPOINT_MAX_AGE_SECONDS - 30)
    path.write_text(_json.dumps(payload))

    assert backend.load_checkpoint("r", state_dir=state_dir) is not None


def test_checkpoint_without_timestamp_is_treated_as_stale(state_dir):
    """A checkpoint from an older build has no updated_ts; do not trust it."""
    import json as _json

    state_dir.mkdir(parents=True, exist_ok=True)
    backend.checkpoint_path("r", state_dir).write_text(
        _json.dumps({"room": "r", "account_number": "HS-10042", "chat_items": []})
    )
    assert backend.load_checkpoint("r", state_dir=state_dir) is None


def test_expiry_can_be_disabled_for_inspection(state_dir):
    import json as _json
    import time as _time

    backend.save_checkpoint(
        room_name="r", account_number="HS-10042", customer_name="P",
        phase="support", chat_items=[], state_dir=state_dir,
    )
    path = backend.checkpoint_path("r", state_dir)
    payload = _json.loads(path.read_text())
    payload["updated_ts"] = _time.time() - 99999
    path.write_text(_json.dumps(payload))

    assert backend.load_checkpoint("r", state_dir=state_dir) is None
    assert backend.load_checkpoint("r", state_dir=state_dir, max_age_seconds=None) is not None


# ---------------------------------------------------------------------------
# Event stream (drives /flow and /stats)
# ---------------------------------------------------------------------------


@pytest.fixture
def events_file(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


def test_emit_event_writes_a_record(events_file):
    record = backend.emit_event(
        "tool_call", session_id="s1", room="r1", tool="search_kb", target="kb", path=events_file
    )
    assert record["kind"] == "tool_call"
    assert record["session_id"] == "s1"
    assert record["tool"] == "search_kb"
    assert isinstance(record["ts"], float)
    assert record["at"]

    rows = backend.read_jsonl(events_file)
    assert len(rows) == 1
    assert rows[0]["target"] == "kb"


def test_emit_event_appends_in_order(events_file):
    for kind in ("call_started", "turn", "session_end"):
        backend.emit_event(kind, path=events_file)
    assert [e["kind"] for e in backend.read_events(path=events_file)] == [
        "call_started",
        "turn",
        "session_end",
    ]


def test_emit_event_never_raises(tmp_path):
    """Telemetry must not be able to break a live call."""
    unwritable = tmp_path / "nope" / "\x00bad" / "events.jsonl"
    assert backend.emit_event("turn", path=unwritable) is None


def test_emit_event_survives_unserialisable_fields(events_file):
    assert backend.emit_event("turn", path=events_file, bad=object()) is None
    # The bad write left nothing behind that would break later reads.
    assert backend.read_events(path=events_file) == []


def test_read_events_since_filters(events_file):
    first = backend.emit_event("call_started", path=events_file)
    backend.emit_event("turn", path=events_file)
    newer = backend.read_events(since=first["ts"], path=events_file)
    assert [e["kind"] for e in newer] == ["turn"]


def test_read_events_since_zero_returns_all(events_file):
    backend.emit_event("call_started", path=events_file)
    assert len(backend.read_events(since=0, path=events_file)) == 1


def test_read_events_respects_limit(events_file):
    for i in range(10):
        backend.emit_event("turn", path=events_file, n=i)
    tail = backend.read_events(limit=3, path=events_file)
    assert [e["n"] for e in tail] == [7, 8, 9]


def test_read_events_missing_file_is_empty(tmp_path):
    assert backend.read_events(path=tmp_path / "none.jsonl") == []
