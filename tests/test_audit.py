import json

from gmail_audit.audit import (
    create_merchant_report,
    extract_json_object,
    keep_screen_result,
    normalize_merchant,
    parse_screen_result,
)
from gmail_audit.oauth import is_invalid_grant
from gmail_audit.recommend import ollama_base_url


# ------------------------------------------------------------
# extract_json_object
# ------------------------------------------------------------

def test_extract_plain_json():
    assert extract_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_fenced_json():
    text = '```json\n{"a": 1}\n```'
    assert json.loads(extract_json_object(text)) == {"a": 1}


def test_extract_embedded_json():
    text = 'Sure! Here you go: {"a": 1} hope that helps'
    assert json.loads(extract_json_object(text)) == {"a": 1}


def test_extract_garbage_returns_none():
    assert extract_json_object("no braces here") is None
    assert extract_json_object("") is None
    assert extract_json_object(None) is None


# ------------------------------------------------------------
# parse_screen_result / keep_screen_result
# ------------------------------------------------------------

def test_parse_unparseable_falls_back_to_keep():
    result = parse_screen_result("garbage", "id1")

    assert result["id"] == "id1"
    assert result["interesting"] is True
    assert result["event_type"] == "uncertain"


def test_parse_confident_irrelevant_is_dropped():
    text = json.dumps({
        "event_type": "irrelevant",
        "interesting": False,
        "merchant_hint": None,
        "confidence": 0.95
    })

    result = parse_screen_result(text, "id2")
    assert result["interesting"] is False


def test_low_confidence_irrelevant_is_kept():
    result = keep_screen_result({
        "event_type": "irrelevant",
        "interesting": False,
        "confidence": 0.3
    })

    assert result["interesting"] is True


def test_non_irrelevant_is_always_kept():
    result = keep_screen_result({
        "event_type": "cancellation",
        "interesting": False,
        "confidence": 0.99
    })

    assert result["interesting"] is True


def test_bad_confidence_value_is_kept():
    result = keep_screen_result({
        "event_type": "irrelevant",
        "interesting": False,
        "confidence": "very"
    })

    assert result["confidence"] == 0.0
    assert result["interesting"] is True


# ------------------------------------------------------------
# merchant report
# ------------------------------------------------------------

def _row(**overrides):
    row = {
        "gmail_id": "g1",
        "internal_date": 1,
        "date_iso": "2025-01-01T00:00:00+00:00",
        "sender": "billing@example.com",
        "subject": "Renewal",
        "merchant": "Acme",
        "category": "software",
        "event_type": "active_renewal",
        "is_recurring": 1,
        "status": "active",
        "amount": 10.0,
        "currency": "USD",
        "cadence": "monthly",
        "renewal_date": None,
        "confidence": 0.9,
        "risk_level": "medium",
        "risk_flags": "[]",
        "summary": "Renews monthly.",
        "evidence": "will renew",
    }
    row.update(overrides)
    return row


def test_merchant_report_aggregates_and_sorts_by_risk():
    rows = [
        _row(merchant="Low Co", risk_level="low"),
        _row(
            merchant="Risky Co",
            internal_date=2,
            risk_level="critical",
            risk_flags='["billing after cancellation"]'
        ),
        _row(
            merchant="Risky Co",
            internal_date=3,
            amount=25.0,
            status="cancelled",
            risk_level="low"
        ),
    ]

    report = create_merchant_report(rows)

    assert [r["merchant"] for r in report] == ["Risky Co", "Low Co"]

    risky = report[0]
    assert risky["highest_risk"] == "critical"
    assert risky["email_events"] == 2
    assert risky["observed_amount"] == 25.0
    assert risky["latest_status"] == "cancelled"
    assert risky["flags"] == "billing after cancellation"


def test_merchant_report_skips_unknown_merchants():
    rows = [
        _row(merchant=None),
        _row(merchant="  "),
    ]

    assert create_merchant_report(rows) == []


def test_normalize_merchant():
    assert normalize_merchant("  Acme   Corp ") == "Acme Corp"
    assert normalize_merchant(None) == "Unknown"


# ------------------------------------------------------------
# ollama_base_url
# ------------------------------------------------------------

def test_ollama_default(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert ollama_base_url() == "http://localhost:11434"


def test_ollama_host_port(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9999")
    assert ollama_base_url() == "http://127.0.0.1:9999"


def test_ollama_bare_host_gets_default_port(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "gpu-box")
    assert ollama_base_url() == "http://gpu-box:11434"


def test_ollama_full_url(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.internal/")
    assert ollama_base_url() == "https://ollama.internal"


# ------------------------------------------------------------
# oauth error classification
# ------------------------------------------------------------

def test_invalid_grant_detection():
    assert is_invalid_grant(
        Exception("invalid_grant: Token has been expired or revoked.")
    )
    assert not is_invalid_grant(Exception("connection reset"))
