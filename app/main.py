"""Web pod: the public-facing pages for paymemoney.

Flow:
  /                -> enter amount + currency, pick Payment / Refund / Withdrawal
  /pay             -> drilldown: choose QR / credit card / bank transfer
  /refund, /withdraw -> simple forms that call the payment pod

This pod never talks to Stripe directly. It forwards to the payment pod
(app/pay.py) at PAYMENT_SERVICE_URL (k8s service DNS name `payment`).
"""

import base64
import io
import os

import httpx
import segno
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse

APP_NAME = os.getenv("APP_NAME", "paymemoney")
# Kubernetes service name of the payment pod. Same namespace, so short name works.
PAY_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment")
# Stripe PUBLISHABLE key (pk_test_...) — safe to expose in the browser by design.
# It only lets Stripe.js tokenize cards; it cannot read data or move money.
# Comes from the `stripe-secret` k8s Secret (injected via `./pmm secret`).
STRIPE_PK = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
CURRENCIES = ["usd", "jpy", "cny"]

# Debug modes -> which payment backend the web pod calls.
#   normal    -> the real payment pod (works)
#   debug-api -> pod with STRIPE_SEND_JSON=1: Stripe calls use json= instead
#                of form-encoding -> every Stripe call fails (API-level bug)
#   debug-k8s -> pod whose Service selector does NOT match its labels:
#                the pod runs fine but the Service has zero endpoints
#                (k8s-level bug; see k8s/debug-k8s-service.yaml)
MODE_URLS = {
    "normal": PAY_URL,
    "debug-api": "http://debug-api",
    "debug-k8s": "http://debug-k8s",
}
MODE_LABELS = {
    "normal": "normal",
    "debug-api": "debug API",
    "debug-k8s": "debug K8s",
}

app = FastAPI(title=APP_NAME, version="0.1.0")

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title} · paymemoney</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#f4f6f8; margin:0; }}
  .card {{ max-width:420px; margin:60px auto; background:#fff; border-radius:12px;
          padding:28px; box-shadow:0 2px 10px rgba(0,0,0,.08); }}
  h1 {{ font-size:1.4rem; margin:0 0 4px; }}
  .sub {{ color:#666; font-size:.85rem; margin-bottom:20px; }}
  label {{ display:block; font-size:.8rem; color:#555; margin:14px 0 4px; }}
  input, select {{ width:100%; padding:10px; border:1px solid #ccd; border-radius:8px;
                  box-sizing:border-box; font-size:1rem; }}
  button, .btn {{ display:block; width:100%; padding:12px; margin-top:16px;
                 border:0; border-radius:8px; background:#4f46e5; color:#fff;
                 font-size:1rem; cursor:pointer; text-align:center;
                 text-decoration:none; box-sizing:border-box; }}
  button.ghost {{ background:#eef0ff; color:#4f46e5; }}
  .row {{ display:flex; gap:10px; }}
  .row > * {{ flex:1; }}
  .methods {{ display:flex; flex-direction:column; gap:10px; margin-top:8px; }}
  .methods button {{ background:#fff; color:#4f46e5; border:1px solid #c7c9f5; }}
  .methods button:hover {{ background:#f0f0ff; }}
  pre {{ background:#f6f6f6; padding:12px; border-radius:8px; font-size:.75rem;
        overflow-x:auto; word-break:break-all; }}
  .err {{ color:#b91c1c; }} .ok {{ color:#15803d; }}
  img.qr {{ display:block; margin:16px auto; width:200px; height:200px; }}
</style></head><body><div class="card">{body}</div></body></html>"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=title, body=body))


def currency_options(selected: str = "usd") -> str:
    return "".join(
        f'<option value="{c}"{" selected" if c == selected else ""}>{c.upper()}</option>'
        for c in CURRENCIES
    )


def backend_url(mode: str) -> str:
    """Pick the payment backend for the selected debug mode."""
    return MODE_URLS.get(mode, MODE_URLS["normal"])


def call_pay(path: str, payload: dict, mode: str = "normal") -> dict:
    """Forward a request to the payment pod and return its JSON."""
    url = backend_url(mode)
    try:
        resp = httpx.post(f"{url}{path}", json=payload, timeout=30)
        return resp.json()
    except httpx.HTTPError as e:
        return {"error": f"cannot reach payment backend {url}: {e}"}


def qr_data_uri(text: str) -> str:
    """Render `text` as a QR code SVG, inlined as a base64 data URI."""
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=4)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def result_html(result: dict, method: str = "") -> str:
    """Pretty-print the payment pod's response.

    Payment endpoints return {"calls": [{path, sent, response}, ...]} — a
    transcript of every Stripe call. We render each call as "sent -> responded"
    so you can see exactly what went over the wire. Plain {"error": ...}
    responses (validation failures, unreachable backend) fall back to simple
    rendering.
    """
    import json

    calls = result.get("calls")
    if calls:
        blocks = []
        for i, c in enumerate(calls, 1):
            blocks.append(
                f"<h2>Stripe call {i}: <code>{c.get('path', '?')}</code></h2>"
                "<div class='sub'>sent to Stripe</div>"
                f"<pre>{json.dumps(c.get('sent'), indent=2)}</pre>"
                "<div class='sub'>Stripe responded</div>"
                f"<pre>{json.dumps(c.get('response'), indent=2)}</pre>"
            )
        last = calls[-1].get("response") or {}
        err = last.get("error") if isinstance(last, dict) else None
        status = (
            f'<p class="err">Stripe returned an error: {err}</p>'
            if err else '<p class="ok">Stripe accepted the request.</p>'
        )
        qr = ""
        # For the QR method, the last response contains a Payment Link URL —
        # QR-encode THAT so scanning opens Stripe's real hosted checkout.
        qr_target = last.get("url") if isinstance(last, dict) else None
        if method == "qr" and qr_target:
            qr = f'<img class="qr" src="{qr_data_uri(qr_target)}" alt="QR code">'
        return (
            "<h1>Result</h1><div class='sub'>Stripe request/response transcript</div>"
            f"{status}{qr}{''.join(blocks)}"
            '<a class="btn ghost" href="/">Back</a>'
        )

    # Fallback: plain error or unexpected shape.
    if result.get("error"):
        status = f'<p class="err">Error: {result["error"]}</p>'
    else:
        status = '<p class="ok">Done.</p>'
    qr = ""
    qr_target = result.get("url") or result.get("id")
    if method == "qr" and qr_target:
        qr = f'<img class="qr" src="{qr_data_uri(qr_target)}" alt="QR code">'
    return (
        "<h1>Result</h1><div class='sub'>payment pod response</div>"
        f"{status}{qr}<pre>{json.dumps(result, indent=2)}</pre>"
        '<a class="btn ghost" href="/">Back</a>'
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return page(
        "paymemoney",
        """
        <h1>paymemoney</h1>
        <div class="sub">Enter an amount, then choose what to do.</div>
        <form method="get" action="/pay">
          <label>Amount</label>
          <input name="amount" type="number" step="0.01" min="0.01" value="10" required>
          <label>Currency</label>
          <select name="currency">__CUR__</select>
          <label>Mode</label>
          <select name="mode">
            <option value="normal" selected>normal</option>
            <option value="debug-api">debug API</option>
            <option value="debug-k8s">debug K8s</option>
          </select>
          <button type="submit">Payment</button>
        </form>
        <div class="row">
          <a class="btn ghost" href="/refund">Refund</a>
          <a class="btn ghost" href="/withdraw">Withdrawal</a>
        </div>
        """.replace("__CUR__", currency_options()),
    )


@app.get("/pay", response_class=HTMLResponse)
def pay_methods(amount: float, currency: str, mode: str = "normal") -> HTMLResponse:
    """Drilldown page: pick how to pay.

    QR needs no input -> POST straight to /pay/qr.
    Card and bank transfer need forms -> GET pages that collect the details.
    `mode` selects which payment backend (normal / debug-api / debug-k8s).
    """
    hidden = (
        f'<input type="hidden" name="amount" value="{amount}">'
        f'<input type="hidden" name="currency" value="{currency}">'
        f'<input type="hidden" name="mode" value="{mode}">'
    )
    hint = ""
    if mode != "normal":
        hint = (f'<div class="sub" style="color:#b45309">Mode: <b>{MODE_LABELS.get(mode, mode)}</b> — '
                'this backend is intentionally broken for troubleshooting practice.</div>')
    body = f"""
        <h1>Pay {amount} {currency.upper()}</h1>
        <div class="sub">Choose a payment method</div>
        {hint}
        <div class="methods">
          <form method="post" action="/pay/qr">{hidden}
            <button type="submit">QR Code</button></form>
          <a class="btn" href="/pay/card?amount={amount}&currency={currency}&mode={mode}">Credit Card</a>
          <a class="btn" href="/pay/bank?amount={amount}&currency={currency}&mode={mode}">Bank Transfer</a>
        </div>
        <a class="btn ghost" href="/">Back</a>
        """
    return page("Choose payment method", body)


@app.post("/pay/qr", response_class=HTMLResponse)
def qr_pay(amount: float = Form(...), currency: str = Form(...), mode: str = Form("normal")):
    """QR flow: payment pod creates a Stripe Payment Link; we render its QR."""
    result = call_pay("/pay/qr", {"amount": amount, "currency": currency}, mode)
    return page("QR payment", result_html(result, "qr"))


@app.get("/pay/card", response_class=HTMLResponse)
def card_form(amount: float, currency: str, mode: str = "normal") -> HTMLResponse:
    """Card form — PCI-safe: Stripe.js tokenizes the card IN THE BROWSER.

    The card number never touches our servers. Stripe.js (loaded from
    js.stripe.com) sends it straight to Stripe and returns a pm_... id,
    which is the only thing we POST to the payment pod.
    """
    # Treat the configmap placeholder as "not configured".
    if not STRIPE_PK or "REPLACE_ME" in STRIPE_PK:
        return page(
            "Credit Card",
            "<h1>Credit Card</h1><p class='err'>STRIPE_PUBLISHABLE_KEY is not set. "
            "Add it to k8s/configmap.yaml and redeploy.</p>"
            f'<a class="btn ghost" href="/pay?amount={amount}&currency={currency}">Back</a>',
        )
    return page(
        f"Pay {amount} {currency.upper()} by card",
        f"""
        <h1>Credit Card</h1>
        <div class="sub">Card details go straight to Stripe — never to our server.</div>
        <form id="pay-form" method="post" action="/pay/card">
          <input type="hidden" name="amount" value="{amount}">
          <input type="hidden" name="currency" value="{currency}">
          <input type="hidden" name="mode" value="{mode}">
          <input type="hidden" name="payment_method" id="pm" value="">
          <label>Card details</label>
          <div id="card-element" style="padding:10px;border:1px solid #ccd;border-radius:8px"></div>
          <div id="card-errors" class="err" style="margin-top:6px;font-size:.85rem"></div>
          <button type="submit">Pay</button>
        </form>
        <a class="btn ghost" href="/pay?amount={amount}&currency={currency}&mode={mode}">Back</a>
        <script src="https://js.stripe.com/v3/"></script>
        <script>
        // Stripe.js with the PUBLISHABLE key (pk_...) — public by design.
        const stripe = Stripe("{STRIPE_PK}");
        const elements = stripe.elements();
        const cardElement = elements.create("card");
        cardElement.mount("#card-element");

        document.getElementById("pay-form").addEventListener("submit", async (e) => {{
          e.preventDefault();
          // Tokenize in the browser: card -> Stripe -> pm_... (no PAN through us)
          const {{paymentMethod, error}} = await stripe.createPaymentMethod({{
            type: "card", card: cardElement,
          }});
          if (error) {{
            document.getElementById("card-errors").textContent = error.message;
            return;
          }}
          document.getElementById("pm").value = paymentMethod.id;
          e.target.submit();  // native submit() skips this listener -> no loop
        }});
        </script>
        """,
    )


@app.post("/pay/card", response_class=HTMLResponse)
def card_pay(
    amount: float = Form(...), currency: str = Form(...),
    payment_method: str = Form(...), mode: str = Form("normal"),
):
    result = call_pay("/pay/card", {
        "amount": amount, "currency": currency,
        "payment_method": payment_method,
    }, mode)
    return page("Payment result", result_html(result))


@app.get("/pay/bank", response_class=HTMLResponse)
def bank_form(amount: float, currency: str, mode: str = "normal") -> HTMLResponse:
    """Bank transfer form — US bank account (ACH-style debit via Stripe)."""
    return page(
        f"Pay {amount} {currency.upper()} by bank transfer",
        f"""
        <h1>Bank Transfer</h1>
        <div class="sub">US bank account (ACH debit). Stripe test mode only
        accepts canned account numbers, so they are fixed — do not edit.</div>
        <form method="post" action="/pay/bank">
          <input type="hidden" name="amount" value="{amount}">
          <input type="hidden" name="currency" value="{currency}">
          <input type="hidden" name="mode" value="{mode}">
          <input type="hidden" name="routing_number" value="110000000">
          <input type="hidden" name="account_number" value="000123456789">
          <label>Account holder name</label>
          <input name="name" placeholder="Jane Doe" required>
          <label>Routing number (fixed test value)</label>
          <input value="110000000" disabled>
          <label>Account number (fixed test value)</label>
          <input value="000123456789" disabled>
          <button type="submit">Pay</button>
        </form>
        <a class="btn ghost" href="/pay?amount={amount}&currency={currency}&mode={mode}">Back</a>
        """,
    )


@app.post("/pay/bank", response_class=HTMLResponse)
def bank_pay(
    amount: float = Form(...), currency: str = Form(...),
    name: str = Form(...), routing_number: str = Form(...),
    account_number: str = Form(...), mode: str = Form("normal"),
):
    result = call_pay("/pay/bank", {
        "amount": amount, "currency": currency, "name": name,
        "routing_number": routing_number, "account_number": account_number,
    }, mode)
    return page("Payment result", result_html(result))


@app.get("/refund", response_class=HTMLResponse)
def refund_form() -> HTMLResponse:
    return page(
        "Refund",
        """
        <h1>Refund</h1>
        <div class="sub">Enter the PaymentIntent id (starts with pi_) from a previous payment.</div>
        <form method="post" action="/refund">
          <label>PaymentIntent ID</label>
          <input name="payment_intent" placeholder="pi_3X..." required>
          <button type="submit">Refund</button>
        </form>
        <a class="btn ghost" href="/">Back</a>
        """,
    )


@app.post("/refund", response_class=HTMLResponse)
def refund(payment_intent: str = Form(...)):
    result = call_pay("/refund", {"payment_intent": payment_intent})
    return page("Refund result", result_html(result))


@app.get("/withdraw", response_class=HTMLResponse)
def withdraw_form() -> HTMLResponse:
    return page(
        "Withdrawal",
        """
        <h1>Withdrawal</h1>
        <div class="sub">Send funds out via a Stripe transfer (needs a connected account).</div>
        <form method="post" action="/withdraw">
          <label>Amount</label>
          <input name="amount" type="number" step="0.01" min="0.01" value="5" required>
          <label>Currency</label>
          <select name="currency">__CUR__</select>
          <button type="submit">Withdraw</button>
        </form>
        <a class="btn ghost" href="/">Back</a>
        """.replace("__CUR__", currency_options()),
    )


@app.post("/withdraw", response_class=HTMLResponse)
def withdraw(amount: float = Form(...), currency: str = Form(...)):
    result = call_pay("/withdraw", {"amount": amount, "currency": currency})
    return page("Withdrawal result", result_html(result))
