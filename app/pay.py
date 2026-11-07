"""Payment pod: the only component that talks to Stripe.

This pod exposes a small JSON API. The web pod (app/main.py) calls it over
the Kubernetes service DNS name `payment`. No HTML lives here.

We call the Stripe REST API directly with httpx (no stripe-python SDK) so the
request/response shapes stay visible for learning. Docs:
https://docs.stripe.com/api

--------------------------------------------------------------------------------
How the Stripe REST API works (the 30-second version)
--------------------------------------------------------------------------------
- Base URL: https://api.stripe.com/v1
- Auth: HTTP Basic — the secret key is the *username*, password is empty.
        That is why we pass auth=(STRIPE_KEY, "") to httpx.
- Requests are application/x-www-form-urlencoded (NOT JSON). That is why we
  pass data={...} (a dict) instead of json={...} to httpx.
- List-style params use bracket notation, e.g. line_items[0][quantity]=1.
  (Newer Stripe API versions removed `payment_method_types` — allowed payment
  methods are configured in the Dashboard instead.)
- Amounts are ALWAYS integers in the smallest currency unit:
  $10.00 USD -> 1000 (cents), ¥1234 JPY -> 1234 (no minor unit).
- Errors come back as JSON with "error": {"type": ..., "message": ...}
  and HTTP status 4xx/5xx. We pass that JSON through untouched.
"""

import os

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

# Secret key comes from the k8s Secret `stripe-secret` (see k8s/secret.yaml,
# created via `./pmm secret`). Test keys start with sk_test_ and cannot move
# real money. Restricted keys start with rk_test_. Never commit a real key.
STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_API = "https://api.stripe.com/v1"

# --- Withdrawals -------------------------------------------------------------
# A Stripe "transfer" moves money from your Stripe balance to a *connected
# account* (Stripe Connect — the "destination" is an acct_... id).
# You must create a test connected account in the dashboard first:
#   https://dashboard.stripe.com/test/connect/accounts
# Until then we return a friendly error instead of calling Stripe.
TRANSFER_DESTINATION = os.getenv("STRIPE_TRANSFER_DESTINATION", "")

# DEBUG KNOB — only non-empty on the `debug-api` pod
# (see k8s/debug-api-deployment.yaml and `./pmm break-api ...`).
# Pick WHERE the API-level bug lives; "" means healthy.
#   ""           -> normal, everything works
#   "json-body"  -> stripe_post sends json= instead of form-encoded data=
#                   -> Stripe rejects the request (encoding bug)
#   "wrong-path" -> calls /paymnt_intents (typo) -> Stripe 404 (routing bug)
#   "no-auth"    -> omits the Authorization header -> Stripe 401 (auth bug)
#   "bad-amount" -> to_smallest_unit forgets *100 -> $10 charges as 10 cents
#                   (a SILENT wrong-amount bug — the sneakiest one)
PAY_BREAK = os.getenv("PAY_BREAK", "")

# Stripe now requires a return_url when confirming a PaymentIntent — where the
# customer lands after redirect-based payment methods. We point it back at our
# own web UI (service DNS name `paymemoney`, port 80). Override with
# STRIPE_RETURN_URL if the app is ever exposed differently.
RETURN_URL = os.getenv("STRIPE_RETURN_URL", "http://paymemoney/")

app = FastAPI(title="paymemoney-pay")

# --- Currency mapping ---------------------------------------------------------
# Our UI offers ISO 4217 codes; Stripe uses the same codes.
#   "cny" = Chinese Yuan (what people often call "rmb").
CURRENCY_MAP = {"usd": "usd", "jpy": "jpy", "cny": "cny"}

# Currencies with NO minor unit — Stripe expects the amount as-is.
# (JPY is the famous one; also KRW, VND, etc. Stripe's full list is in their docs.)
ZERO_DECIMAL = {"jpy"}

# --- Payment methods ----------------------------------------------------------
# Our UI offers three buttons. The "allowed payment methods" for a
# PaymentIntent are now configured in the Stripe Dashboard (the API no longer
# accepts payment_method_types), so this map is only used to VALIDATE the
# method name — each flow below passes a concrete pm_... instead.
#
#   card           -> real card via Stripe.js tokenization in the browser
#   qr             -> a Payment Link (hosted checkout); the web pod QR-encodes
#                     the URL — QR is a display format, not a Stripe type.
# (bank_transfer was removed: this Stripe account has no ACH capability.)
METHOD_MAP = {
    "card": ["card"],
    "qr": ["payment_link"],
}


def to_smallest_unit(amount: float, currency: str) -> int:
    """Convert a human amount (e.g. 12.34) to Stripe's smallest unit.

    Stripe NEVER accepts decimals: $12.34 must be sent as 1234 (cents).
    JPY has no cents, so ¥1234 is sent as 1234.
    """
    if PAY_BREAK == "bad-amount":
        # BUG (debug-api pod only): forgets the *100 -> charges 1/100 of the
        # intended amount. Stripe accepts it happily — the bug is silent.
        return int(amount)
    if currency in ZERO_DECIMAL:
        return int(amount)
    return int(round(amount * 100))


def stripe_post(path: str, data: dict) -> dict:
    """POST form-encoded data to Stripe and return the parsed JSON body.

    Examples of `path`:
      /payment_intents  -> create a PaymentIntent (the core "charge" object)
      /refunds          -> refund a charge/payment intent
      /transfers        -> move money to a connected account (withdrawal)

    `data` is sent as application/x-www-form-urlencoded, which is what Stripe
    expects (their SDKs do this for you; we do it by hand to see it).
    """
    if not STRIPE_KEY:
        return {"error": "STRIPE_SECRET_KEY is not set (check the stripe-secret k8s Secret)"}

    # --- debug-api break points (PAY_BREAK env var) -------------------------
    if PAY_BREAK == "wrong-path":
        path = "/paymnt_intents"  # BUG: typo -> Stripe 404
    kwargs = {"timeout": 30}
    if PAY_BREAK == "json-body":
        kwargs["json"] = data          # BUG: Stripe wants form-encoding, not JSON
    elif PAY_BREAK == "no-auth":
        kwargs["data"] = data          # BUG: no auth -> Stripe 401
    else:
        kwargs["data"] = data          # dict -> form-encoded body (NOT json=)
        kwargs["auth"] = (STRIPE_KEY, "")  # HTTP Basic: key as username, empty pw
    resp = httpx.post(f"{STRIPE_API}{path}", **kwargs)
    # Stripe returns 2xx on success and 4xx/5xx with a JSON error body like:
    #   {"error": {"type": "invalid_request_error", "message": "..."}}
    # We pass the body through either way so the web pod can display it.
    return resp.json()


def stripe_call(path: str, data: dict) -> dict:
    """One Stripe API call, returned as a transcript entry:

        {"path": "/payment_intents",
         "sent":      {<exact form fields we POSTed>},
         "response":  {<Stripe's JSON reply>}}

    Endpoints wrap their calls in {"calls": [...]} so the web page can show
    BOTH sides of the wire — what we sent and what Stripe answered.
    """
    return {"path": path, "sent": data, "response": stripe_post(path, data)}


class PayRequest(BaseModel):
    amount: float      # human units, e.g. 10.50
    currency: str      # our codes: usd | jpy | cny
    method: str        # our codes: qr | card


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/pay")
def pay(req: PayRequest) -> dict:
    """Create AND confirm a Stripe PaymentIntent in one call.

    A PaymentIntent is Stripe's central object for a payment. Its lifecycle:
      requires_payment_method -> requires_confirmation -> requires_action
      -> processing -> succeeded  (or -> canceled / requires_payment_method on failure)

    Fields we send (all form-encoded):
      amount           smallest currency unit, as a string
      currency         ISO 4217 lowercase
      payment_method   which concrete method to use (pm_... id)
      confirm          "true" = charge immediately, don't just create the intent

    NOTE: newer Stripe API versions removed `payment_method_types` — allowed
    payment methods now come from Dashboard settings via "automatic payment
    methods". Some Dashboard-enabled methods can redirect the customer, which
    would need a return_url; we disable redirects since card/ACH don't need
    them (see automatic_payment_methods below).
    """
    currency = CURRENCY_MAP.get(req.currency.lower())
    pmt_types = METHOD_MAP.get(req.method)
    if not currency or not pmt_types:
        return {"error": f"unsupported currency/method: {req.currency}/{req.method}"}

    data = {
        "amount": str(to_smallest_unit(req.amount, currency)),
        "currency": currency,
        # pm_card_visa is Stripe's built-in TEST payment method that always
        # succeeds — no real card number needed in test mode.
        # Real cards would come from Stripe.js / Payment Element on the page.
        "payment_method": "pm_card_visa",
        "confirm": "true",
        # New-API requirement: Dashboard-managed payment methods may include
        # redirect-based ones; we accept only non-redirecting methods.
        "automatic_payment_methods[enabled]": "true",
        "automatic_payment_methods[allow_redirects]": "never",
        # Stripe requires this at confirm time even when redirects are off.
        "return_url": RETURN_URL,
    }
    # POST /v1/payment_intents  ->  creates the intent and, because confirm=true,
    # immediately tries to charge it. Response contains "id": "pi_...",
    # "status": "succeeded", "client_secret": "...", etc.
    return {"calls": [stripe_call("/payment_intents", data)]}


# ---------------------------------------------------------------------------
# Real per-method flows.
#
# PCI scope note: raw card numbers are NEVER sent to this pod. The browser
# tokenizes the card with Stripe.js (publishable key) and sends us only a
# PaymentMethod id (pm_...). Our job is just to confirm a PaymentIntent
# with that pm_... — one Stripe call.
# ---------------------------------------------------------------------------


class MoneyRequest(BaseModel):
    amount: float
    currency: str


class CardPayRequest(MoneyRequest):
    payment_method: str  # pm_... created client-side by Stripe.js in the browser


@app.post("/pay/card")
def pay_card(req: CardPayRequest) -> dict:
    """Charge a card tokenized in the browser.

    The web page uses Stripe.js Card Element + the publishable key to turn
    the typed card into a PaymentMethod (pm_...) WITHOUT the number ever
    leaving the browser->Stripe channel. This endpoint only confirms:

      POST /v1/payment_intents
        payment_method=pm_...
        confirm=true

    If the pm_... is invalid/expired Stripe returns an error JSON which we
    pass through to the result page.
    """
    currency = CURRENCY_MAP.get(req.currency.lower())
    if not currency:
        return {"error": f"unsupported currency: {req.currency}"}

    return {"calls": [stripe_call("/payment_intents", {
        "amount": str(to_smallest_unit(req.amount, currency)),
        "currency": currency,
        "payment_method": req.payment_method,
        "confirm": "true",
        "automatic_payment_methods[enabled]": "true",
        "automatic_payment_methods[allow_redirects]": "never",
        "return_url": RETURN_URL,
    })]}


@app.post("/pay/qr")
def pay_qr(req: MoneyRequest) -> dict:
    """Create a Stripe Payment Link — a hosted checkout URL.

    POST /v1/payment_links with an inline price (price_data) creates a
    shareable https://buy.stripe.com/... URL. The web pod turns that URL
    into a QR code; scanning it opens Stripe's own checkout page where the
    customer actually pays. No card data ever touches our servers.

    Response contains "url" — that is what we QR-encode.
    """
    currency = CURRENCY_MAP.get(req.currency.lower())
    if not currency:
        return {"error": f"unsupported currency: {req.currency}"}

    return {"calls": [stripe_call("/payment_links", {
        "line_items[0][price_data][currency]": currency,
        "line_items[0][price_data][unit_amount]": str(
            to_smallest_unit(req.amount, currency)
        ),
        "line_items[0][price_data][product_data][name]": "paymemoney",
        "line_items[0][quantity]": "1",
    })]}


class RefundRequest(BaseModel):
    payment_intent: str  # e.g. pi_3Xxxxx — the "id" from a previous /pay response


@app.post("/refund")
def refund(req: RefundRequest) -> dict:
    """Refund via POST /v1/refunds.

    You can refund by `payment_intent` (preferred — refunds the latest charge
    on that intent) or by `charge` (ch_...) or by `payment_intent` + `amount`
    for partial refunds. We keep it simple: full refund of the intent.
    """
    return {"calls": [stripe_call("/refunds", {"payment_intent": req.payment_intent})]}


class WithdrawRequest(BaseModel):
    amount: float
    currency: str


@app.post("/withdraw")
def withdraw(req: WithdrawRequest) -> dict:
    """Send money out via POST /v1/transfers.

    A transfer moves funds from YOUR Stripe balance to a connected account
    (Stripe Connect). It is NOT a payout to a bank — payouts are a different
    API (/v1/payouts) and require a verified bank account on your own account.

    Fields:
      amount      smallest currency unit
      currency    ISO 4217
      destination acct_... id of the connected account receiving the money
    """
    if not TRANSFER_DESTINATION:
        return {
            "error": "STRIPE_TRANSFER_DESTINATION is empty. "
            "Create a test connected account in the Stripe dashboard and set it."
        }
    currency = CURRENCY_MAP.get(req.currency.lower(), "usd")
    return {"calls": [stripe_call("/transfers", {
        "amount": str(to_smallest_unit(req.amount, currency)),
        "currency": currency,
        "destination": TRANSFER_DESTINATION,
    })]}
