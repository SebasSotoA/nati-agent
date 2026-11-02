"""WhatsApp adapter: signature, allowlist, payload extraction. No network."""

from __future__ import annotations

import hashlib
import hmac

from nati.channels import sessions, whatsapp


def test_signature_accepts_a_matching_hmac() -> None:
    secret = "app-secret"
    body = b'{"object":"whatsapp_business_account"}'
    header = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert whatsapp.signature_ok(body, header, secret)


def test_signature_rejects_a_wrong_hmac() -> None:
    body = b"{}"
    header = "sha256=" + hmac.new(b"other", body, hashlib.sha256).hexdigest()
    assert not whatsapp.signature_ok(body, header, "app-secret")


def test_signature_rejects_a_missing_prefix() -> None:
    assert not whatsapp.signature_ok(b"{}", "deadbeef", "secret")


def test_same_phone_matches_local_and_country_code() -> None:
    assert whatsapp.same_phone("573101234567", "3101234567")
    assert whatsapp.same_phone("3101234567", "573101234567")
    assert whatsapp.same_phone("573101234567", "573101234567")
    assert not whatsapp.same_phone("3101234567", "3109999999")


def test_same_phone_rejects_a_suffix_collision() -> None:
    """A different country prefix sharing the last 10 digits is not the admin."""
    assert not whatsapp.same_phone("123101234567", "3101234567")
    assert not whatsapp.same_phone("3101234567", "1")
    assert not whatsapp.same_phone("", "3101234567")


def test_extract_text_reads_the_first_inbound_message() -> None:
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.TEST1",
                        "from": "573101234567",
                        "type": "text",
                        "text": {"body": "Cuanto hay en caja?"},
                    }]
                }
            }]
        }]
    }
    assert whatsapp.extract_text(payload) == (
        "573101234567", "Cuanto hay en caja?", "wamid.TEST1"
    )


def test_extract_text_ignores_status_callbacks() -> None:
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid"}]}}]}]}
    assert whatsapp.extract_text(payload) is None


def test_split_reply_keeps_short_text() -> None:
    assert whatsapp.split_reply("hola") == ["hola"]


def test_split_reply_chunks_long_text() -> None:
    parts = whatsapp.split_reply("abcdefghij", limit=4)
    assert parts == ["abcd", "efgh", "ij"]


def test_sessions_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nati.config.VAR_DIR", tmp_path)
    sessions.save("573101234567", "20260822T120000Z-abc123", [{"role": "user", "content": "hi"}])
    conv_id, history = sessions.load("573101234567")
    assert conv_id == "20260822T120000Z-abc123"
    assert history == [{"role": "user", "content": "hi"}]
    assert sessions.load("unknown") == (None, None)


def test_claim_message_is_single_use(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nati.config.VAR_DIR", tmp_path)
    assert sessions.claim_message("wamid.ONCE") is True
    assert sessions.claim_message("wamid.ONCE") is False
    assert sessions.claim_message("") is True


def test_compose_outbound_appends_confirmation_summaries() -> None:
    body = whatsapp.compose_outbound(
        "I need you to confirm.",
        [{"summary": "Archive 10 savings for 2026. Members stay."}],
    )
    assert "I need you to confirm." in body
    assert "Archive 10 savings for 2026" in body
