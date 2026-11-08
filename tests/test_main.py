"""Unit tests for app/main.py — the web pod.

call_pay is monkeypatched so tests never touch the payment pod or Stripe.
"""

import app.main as main_mod
from fastapi.testclient import TestClient

client = TestClient(main_mod.app)


# --- qr_data_uri -------------------------------------------------------------

def test_qr_data_uri_is_svg_data_uri():
    uri = main_mod.qr_data_uri("hello")
    assert uri.startswith("data:image/svg+xml;base64,")


# --- GET pages ---------------------------------------------------------------

def test_home_has_mode_selector():
    html = client.get("/").text
    assert 'name="mode"' in html
    for m in ("normal", "debug-api", "debug-k8s"):
        assert f'value="{m}"' in html
    assert 'value="normal" selected' in html  # normal is the default


def test_backend_url_modes():
    assert main_mod.backend_url("normal") == "http://payment"
    assert main_mod.backend_url("debug-api") == "http://debug-api"
    assert main_mod.backend_url("debug-k8s") == "http://debug-k8s"
    assert main_mod.backend_url("bogus") == "http://payment"  # safe fallback


def test_pay_drilldown_in_debug_mode_shows_hint_and_carries_mode():
    html = client.get("/pay?amount=10&currency=usd&mode=debug-k8s").text
    assert "intentionally broken" in html
    assert 'name="mode" value="debug-k8s"' in html
    assert "mode=debug-k8s" in html  # carried into card links
    assert 'action="/debug/deploy"' in html  # Deploy button present


def test_pay_drilldown_normal_mode_has_no_deploy_button():
    html = client.get("/pay?amount=10&currency=usd&mode=normal").text
    assert "/debug/deploy" not in html


def test_debug_deploy_proxies_to_ctl(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        main_mod, "call_ctl",
        lambda mode: captured.update(mode=mode) or {"mode": mode, "scenario": "wrong-path"},
    )
    resp = client.post("/debug/deploy", data={
        "amount": "10", "currency": "usd", "mode": "debug-api",
    })
    assert captured["mode"] == "debug-api"
    assert "wrong-path" in resp.text
    assert "/pay?amount=10.0&currency=usd&mode=debug-api" in resp.text


def test_qr_pay_passes_mode_to_backend(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        main_mod, "call_pay",
        lambda path, payload, mode="normal": captured.update(mode=mode) or {"url": "u"},
    )
    client.post("/pay/qr", data={"amount": "1", "currency": "usd", "mode": "debug-api"})
    assert captured["mode"] == "debug-api"


def test_home_shows_form_and_currencies():
    html = client.get("/").text
    assert "paymemoney" in html
    for c in ("usd", "jpy", "cny"):
        assert f'value="{c}"' in html
    assert 'action="/pay"' in html
    assert 'href="/refund"' in html and 'href="/withdraw"' in html


def test_pay_drilldown_lists_methods():
    html = client.get("/pay?amount=10&currency=usd").text
    assert "Pay 10.0 USD" in html
    assert 'action="/pay/qr"' in html
    assert "/pay/card?amount=10.0&currency=usd" in html
    # bank transfer was removed — this Stripe account has no ACH capability
    assert "/pay/bank" not in html


def test_card_form_uses_stripe_js(monkeypatch):
    monkeypatch.setattr(main_mod, "STRIPE_PK", "pk_test_fake")
    html = client.get("/pay/card?amount=10&currency=usd").text
    assert "js.stripe.com/v3" in html
    assert 'Stripe("pk_test_fake")' in html
    assert 'id="card-element"' in html
    assert 'name="payment_method"' in html
    # raw card inputs must NOT exist — PCI: numbers never touch our server
    assert 'name="number"' not in html and 'name="cvc"' not in html


def test_card_form_warns_without_publishable_key(monkeypatch):
    monkeypatch.setattr(main_mod, "STRIPE_PK", "")
    html = client.get("/pay/card?amount=10&currency=usd").text
    assert "STRIPE_PUBLISHABLE_KEY is not set" in html


def test_refund_form():
    assert "PaymentIntent" in client.get("/refund").text


def test_withdraw_form():
    assert "Withdrawal" in client.get("/withdraw").text


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


# --- POST endpoints (payment pod is faked) ------------------------------------

def test_qr_pay_shows_qr_of_payment_link(monkeypatch):
    monkeypatch.setattr(
        main_mod, "call_pay",
        lambda path, payload, mode="normal": {"url": "https://buy.stripe.com/test_abc"},
    )
    resp = client.post("/pay/qr", data={"amount": "10", "currency": "jpy"})
    assert resp.status_code == 200
    assert 'class="qr"' in resp.text
    assert "data:image/svg+xml;base64," in resp.text


def test_card_pay_forwards_pm_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        main_mod, "call_pay",
        lambda path, payload, mode="normal": captured.update(path=path, payload=payload) or {"id": "pi_x"},
    )
    resp = client.post("/pay/card", data={
        "amount": "10", "currency": "usd", "payment_method": "pm_abc123",
    })
    assert captured["path"] == "/pay/card"
    assert captured["payload"] == {
        "amount": 10.0, "currency": "usd", "payment_method": "pm_abc123",
    }
    assert "pi_x" in resp.text


def test_refund_post(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        main_mod, "call_pay",
        lambda path, payload, mode="normal": captured.update(payload=payload) or {"id": "re_1"},
    )
    resp = client.post("/refund", data={"payment_intent": "pi_abc"})
    assert captured["payload"] == {"payment_intent": "pi_abc"}
    assert "re_1" in resp.text


def test_withdraw_post(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        main_mod, "call_pay",
        lambda path, payload, mode="normal": captured.update(payload=payload) or {"id": "tr_1"},
    )
    resp = client.post("/withdraw", data={"amount": "5", "currency": "usd"})
    assert captured["payload"]["amount"] == 5.0
    assert "tr_1" in resp.text


def test_call_pay_surfaces_payment_pod_error(monkeypatch):
    """If the payment pod is unreachable, the page shows an error, not a crash."""
    import httpx

    def boom(*args, **kwargs):
        raise httpx.ConnectError("refused")

    # call_pay catches httpx.HTTPError internally — exercise it directly.
    monkeypatch.setattr(main_mod.httpx, "post", boom)
    result = main_mod.call_pay("/pay", {"amount": 1, "currency": "usd", "method": "card"})
    assert "cannot reach payment backend" in result["error"]


def test_result_page_renders_stripe_transcript(monkeypatch):
    monkeypatch.setattr(
        main_mod, "call_pay",
        lambda path, payload, mode="normal": {"calls": [{
            "path": "/payment_intents",
            "sent": {"amount": "1000", "currency": "usd"},
            "response": {"id": "pi_t", "status": "succeeded"},
        }]},
    )
    resp = client.post("/pay/card", data={
        "amount": "10", "currency": "usd", "payment_method": "pm_x",
    })
    assert "sent to Stripe" in resp.text
    assert "Stripe responded" in resp.text
    assert "payment_intents" in resp.text
    assert "succeeded" in resp.text


def test_result_page_shows_stripe_error(monkeypatch):
    monkeypatch.setattr(
        main_mod, "call_pay",
        lambda path, payload, mode="normal": {"error": {"message": "Invalid API Key"}},
    )
    resp = client.post("/pay/card", data={
        "amount": "1", "currency": "usd", "payment_method": "pm_bad",
    })
    assert "Error:" in resp.text
