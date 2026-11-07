"""Unit tests for app/pay.py — the Stripe-calling pod.

Strategy: use FastAPI's TestClient to exercise the HTTP endpoints, and
monkeypatch stripe_post so no real Stripe call ever happens.
"""

import app.pay as pay_mod
from fastapi.testclient import TestClient

client = TestClient(pay_mod.app)


# --- to_smallest_unit --------------------------------------------------------

def test_smallest_unit_usd():
    assert pay_mod.to_smallest_unit(10.50, "usd") == 1050


def test_smallest_unit_jpy_has_no_minor_unit():
    assert pay_mod.to_smallest_unit(1234, "jpy") == 1234


def test_smallest_unit_cny():
    assert pay_mod.to_smallest_unit(8.88, "cny") == 888


# --- /health -----------------------------------------------------------------

def test_health():
    assert client.get("/health").json() == {"status": "ok"}


# --- /pay --------------------------------------------------------------------

def test_pay_rejects_bad_currency():
    resp = client.post("/pay", json={"amount": 1, "currency": "eur", "method": "card"})
    assert "unsupported" in resp.json()["error"]


def test_pay_rejects_bad_method():
    resp = client.post("/pay", json={"amount": 1, "currency": "usd", "method": "crypto"})
    assert "unsupported" in resp.json()["error"]


def test_pay_sends_expected_stripe_fields(monkeypatch):
    """Verify the form data we'd POST to Stripe is correct."""
    captured = {}

    def fake_stripe_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {"id": "pi_test", "status": "succeeded"}

    monkeypatch.setattr(pay_mod, "stripe_post", fake_stripe_post)

    resp = client.post("/pay", json={"amount": 12.34, "currency": "usd", "method": "card"})

    assert resp.json()["calls"][0]["response"]["status"] == "succeeded"
    assert captured["path"] == "/payment_intents"
    assert captured["data"]["amount"] == "1234"          # cents, as string
    assert captured["data"]["currency"] == "usd"
    assert captured["data"]["payment_method_types[0]"] == "card"
    assert captured["data"]["confirm"] == "true"
    assert captured["data"]["payment_method"] == "pm_card_visa"


def test_pay_rmb_maps_to_cny_and_jpy_stays_whole(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        pay_mod, "stripe_post",
        lambda path, data: captured.update(data=data) or {"ok": True},
    )
    # note: cny is now a first-class code (rmb was renamed); check both paths
    client.post("/pay", json={"amount": 5, "currency": "cny", "method": "card"})
    assert captured["data"]["currency"] == "cny"
    assert captured["data"]["amount"] == "500"

    client.post("/pay", json={"amount": 500, "currency": "jpy", "method": "qr"})
    assert captured["data"]["amount"] == "500"  # no *100 for jpy


# --- /pay/card, /pay/bank, /pay/qr (two-step Stripe flows) ---------------------

def fake_stripe_sequence(returns):
    """stripe_post fake that returns queued responses and records calls."""
    calls = []

    def fake(path, data):
        calls.append((path, data))
        return returns[len(calls) - 1]

    return calls, fake


def test_pay_card_confirms_with_pm(monkeypatch):
    """Card pay receives a pm_... (tokenized by Stripe.js) and confirms it."""
    calls, fake = fake_stripe_sequence([
        {"id": "pi_1", "status": "succeeded"},
    ])
    monkeypatch.setattr(pay_mod, "stripe_post", fake)

    resp = client.post("/pay/card", json={
        "amount": 10, "currency": "usd", "payment_method": "pm_visa123",
    })

    assert resp.json()["calls"][0]["response"]["status"] == "succeeded"
    # exactly ONE Stripe call — no raw card data ever reaches this pod
    assert [c[0] for c in calls] == ["/payment_intents"]
    assert calls[0][1]["payment_method"] == "pm_visa123"
    assert calls[0][1]["payment_method_types[0]"] == "card"
    assert calls[0][1]["confirm"] == "true"


def test_pay_card_rejects_bad_currency(monkeypatch):
    resp = client.post("/pay/card", json={
        "amount": 10, "currency": "eur", "payment_method": "pm_x",
    })
    assert "unsupported" in resp.json()["error"]


def test_pay_bank_uses_us_bank_account(monkeypatch):
    calls, fake = fake_stripe_sequence([
        {"id": "pm_bank"},
        {"id": "pi_bank", "status": "requires_action"},
    ])
    monkeypatch.setattr(pay_mod, "stripe_post", fake)

    resp = client.post("/pay/bank", json={
        "amount": 20, "currency": "usd", "name": "Jane Doe",
        "routing_number": "110000000", "account_number": "000123456789",
    })

    assert resp.json()["calls"][-1]["response"]["status"] == "requires_action"
    assert calls[0][1]["type"] == "us_bank_account"
    assert calls[0][1]["us_bank_account[routing_number]"] == "110000000"
    assert calls[1][1]["payment_method_types[0]"] == "us_bank_account"


def test_pay_qr_creates_payment_link(monkeypatch):
    calls, fake = fake_stripe_sequence([
        {"id": "plink_1", "url": "https://buy.stripe.com/test_abc"},
    ])
    monkeypatch.setattr(pay_mod, "stripe_post", fake)

    resp = client.post("/pay/qr", json={"amount": 15.5, "currency": "usd"})

    assert resp.json()["calls"][0]["response"]["url"] == "https://buy.stripe.com/test_abc"
    assert calls[0][0] == "/payment_links"
    assert calls[0][1]["line_items[0][price_data][unit_amount]"] == "1550"
    assert calls[0][1]["line_items[0][price_data][currency]"] == "usd"


# --- PAY_BREAK debug variants -------------------------------------------------

class FakeResp:
    def json(self):
        return {"ok": True}


def capture_post(monkeypatch, break_value):
    """Run stripe_post with PAY_BREAK set; return the kwargs httpx.post got."""
    captured = {}
    monkeypatch.setattr(pay_mod, "PAY_BREAK", break_value)
    monkeypatch.setattr(pay_mod, "STRIPE_KEY", "sk_test_x")

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResp()

    monkeypatch.setattr(pay_mod.httpx, "post", fake_post)
    pay_mod.stripe_post("/payment_intents", {"amount": "100"})
    return captured


def test_pay_break_json_body(monkeypatch):
    kw = capture_post(monkeypatch, "json-body")
    assert "json" in kw and "data" not in kw and "auth" not in kw


def test_pay_break_no_auth(monkeypatch):
    kw = capture_post(monkeypatch, "no-auth")
    assert "data" in kw and "auth" not in kw


def test_pay_break_wrong_path(monkeypatch):
    kw = capture_post(monkeypatch, "wrong-path")
    assert kw["url"].endswith("/paymnt_intents")


def test_pay_break_bad_amount(monkeypatch):
    monkeypatch.setattr(pay_mod, "PAY_BREAK", "bad-amount")
    # $10.00 -> 10 (not 1000): the silent 1/100 bug
    assert pay_mod.to_smallest_unit(10.0, "usd") == 10


def test_pay_break_healthy(monkeypatch):
    kw = capture_post(monkeypatch, "")
    assert "data" in kw and "auth" in kw and "json" not in kw


def test_endpoints_return_transcript_shape(monkeypatch):
    """Every payment endpoint returns {"calls": [{path, sent, response}]}."""
    calls, fake = fake_stripe_sequence([{"id": "pi_t", "status": "succeeded"}])
    monkeypatch.setattr(pay_mod, "stripe_post", fake)
    resp = client.post("/pay/card", json={
        "amount": 1, "currency": "usd", "payment_method": "pm_x",
    })
    entry = resp.json()["calls"][0]
    assert entry["path"] == "/payment_intents"
    assert entry["sent"]["payment_method"] == "pm_x"
    assert entry["response"]["status"] == "succeeded"


# --- /refund -----------------------------------------------------------------

def test_refund_passes_payment_intent(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        pay_mod, "stripe_post",
        lambda path, data: captured.update(path=path, data=data) or {"id": "re_1"},
    )
    resp = client.post("/refund", json={"payment_intent": "pi_abc"})
    assert resp.json()["calls"][0]["response"]["id"] == "re_1"
    assert captured["path"] == "/refunds"
    assert captured["data"] == {"payment_intent": "pi_abc"}


# --- /withdraw ---------------------------------------------------------------

def test_withdraw_requires_destination(monkeypatch):
    monkeypatch.setattr(pay_mod, "TRANSFER_DESTINATION", "")
    resp = client.post("/withdraw", json={"amount": 5, "currency": "usd"})
    assert "STRIPE_TRANSFER_DESTINATION" in resp.json()["error"]


def test_withdraw_calls_transfers(monkeypatch):
    captured = {}
    monkeypatch.setattr(pay_mod, "TRANSFER_DESTINATION", "acct_test123")
    monkeypatch.setattr(
        pay_mod, "stripe_post",
        lambda path, data: captured.update(path=path, data=data) or {"id": "tr_1"},
    )
    resp = client.post("/withdraw", json={"amount": 2.5, "currency": "usd"})
    assert resp.json()["calls"][0]["response"]["id"] == "tr_1"
    assert captured["path"] == "/transfers"
    assert captured["data"]["destination"] == "acct_test123"
    assert captured["data"]["amount"] == "250"
