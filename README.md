# paymemoney

A learning project: a small payment app running on local Kubernetes (minikube + Docker).
Python/FastAPI backend, Stripe as the payment provider via its REST API.

## Architecture

```
┌──────────────────┐      ┌──────────────────┐      ┌───────────────┐
│  paymemoney pod  │ ───▶ │   payment pod    │ ───▶ │  Stripe API   │
│  (web, 2 replicas)│     │  (1 replica)     │      │  (REST, no    │
│  HTML + Stripe.js│      │  Stripe REST only│      │   SDK)        │
└──────────────────┘      └──────────────────┘      └───────────────┘
        NodePort :30081            ClusterIP only

   debug backends (selected via the Mode dropdown on /):
   debug-api pod  -> same code, PAY_BREAK env injects an API-level bug
   debug-k8s pod  -> healthy pod, break-k8s.sh breaks its Service live
```

- **`app/main.py`** — web pod. Serves the pages (incl. Stripe.js card
  tokenization) and forwards payment calls to a payment backend chosen by the
  Mode dropdown: `payment`, `debug-api`, or `debug-k8s`.
- **`app/pay.py`** — payment pod code (shared by `payment`, `debug-api`,
  `debug-k8s`). The only component that talks to Stripe (`payment_intents`,
  `payment_links`, `payment_methods`, `refunds`, `transfers`).
- All pods run from the **same Docker image**; each deployment overrides the
  uvicorn target or env.
- Pods talk over Kubernetes service DNS: `http://payment`, `http://debug-api`,
  `http://debug-k8s` (see `MODE_URLS` in `app/main.py`).

## Pages

| Page | What it does |
|---|---|
| `/` | Amount + currency + **Mode** dropdown (normal / debug API / debug K8s); Payment / Refund / Withdrawal |
| `/pay` | Drilldown: pick **QR Code**, **Credit Card**, or **Bank Transfer** (carries the mode) |
| `/pay/card` | Stripe.js Card Element tokenizes in-browser → posts only `pm_...` → PaymentIntent confirm |
| `/pay/bank` | Bank account form → `us_bank_account` PaymentMethod + intent |
| `/pay/qr` | Creates a Stripe **Payment Link**, renders it as a scannable QR |
| `/refund` | Paste a `pi_...` PaymentIntent id and refund it |
| `/withdraw` | Send funds out via a Stripe transfer (needs a Connect account) |

### Debug modes (troubleshooting practice)

The entry page has a **Mode** dropdown: `normal` (default), `debug API`,
`debug K8s`. The choice follows you through the whole payment flow and picks
which backend pod the web pod calls. **Neither mode has a fixed bug** — you
move the fault around yourself:

**`debug API`** — API-level bugs inside the pod, selected by the `PAY_BREAK`
env var (documented in `app/pay.py`):

```sh
./pmm break-api json-body     # sends JSON instead of form-encoding -> Stripe rejects
./pmm break-api wrong-path    # calls /paymnt_intents (typo) -> Stripe 404
./pmm break-api no-auth       # omits the auth header -> Stripe 401
./pmm break-api bad-amount    # forgets *100 -> $10 charges as 10 cents (silent!)
./pmm break-api none          # heal: debug-api becomes fully healthy
```

Changing `TARGET` rolls the pod automatically. Troubleshooting path: payment
fails → `kubectl logs deploy/debug-api` → Stripe's error JSON → compare with
what `stripe_post` *should* send → find the `PAY_BREAK` branch.

**`debug K8s`** — cluster-level bugs patched onto the live objects by
`break-k8s.sh` (the committed manifests stay correct, so `./pmm deploy` always
restores a healthy baseline):

```sh
./pmm break-k8s selector   # svc selector matches nothing -> 0 endpoints
./pmm break-k8s port       # targetPort 9999 -> endpoints up, conn refused
./pmm break-k8s probe      # probes hit /broken -> CrashLoopBackOff
./pmm break-k8s scale0     # 0 replicas -> no pods at all
./pmm break-k8s reset      # heal everything
```

Troubleshooting path: payment fails → `kubectl get pods` (Running!) →
`kubectl get svc` → `kubectl get endpoints debug-k8s` → `kubectl describe` →
find which layer is lying.

Note: with an invalid/missing Stripe key, API bugs can be masked — Stripe
validates the key before the body. Set a real `sk_test_` key (`./pmm secret`)
to see encoding/routing bugs surface distinctly.

**QR** — the payment pod calls `POST /v1/payment_links` with an inline price,
producing a real `https://buy.stripe.com/...` URL. The web pod QR-encodes that
URL, so scanning it opens Stripe's hosted checkout — scanning actually pays.

**Card** — PCI-safe flow: the page loads **Stripe.js** and mounts a Card
Element. On submit, `stripe.createPaymentMethod()` tokenizes the card **in the
browser** (card number goes browser → Stripe directly, never through our
pods) and returns a `pm_...` id. Only that id is POSTed to the payment pod,
which confirms a PaymentIntent — one Stripe call.

Two keys are involved, on purpose:

| Key | Where it lives | Can do |
|---|---|---|
| `pk_test_...` publishable | `stripe-secret` Secret → web pod env → browser (public by design) | tokenize cards via Stripe.js only |
| `sk_test_...` secret | `stripe-secret` Secret → payment pods only | everything (charges, refunds, transfers, payment links) |

Both keys are injected with `./pmm secret` — nothing key-shaped is committed
to the repo (not even the publishable one). Without `pk_`, only the card page
is affected; everything else works.

**Bank transfer** — collects holder name + US routing/account number;
creates a `us_bank_account` PaymentMethod. Bank debits are not instant:
Stripe returns `requires_action` while the debit is verified/authorized —
that is expected, not an error.

## Prerequisites

- macOS with [Homebrew](https://brew.sh)
- Everything else is installed/upgraded by the setup script:

```sh
./setup.sh
```

It checks `docker` (Docker Desktop), `minikube`, `kubectl`, `python3` —
installs what's missing via brew, upgrades what's outdated, starts Docker Desktop
if needed, and starts minikube. Safe to re-run anytime (`./setup.sh --no-start`
skips starting the cluster).

## Setup

```sh
# 1. Prerequisites + start minikube
./setup.sh

# 2. Inject your Stripe TEST keys (get them at
#    https://dashboard.stripe.com/test/apikeys)
#    - sk_test_... secret key: required for everything server-side
#    - pk_test_... publishable key: needed only for card payments (Stripe.js)
#    Neither is ever written to a repo file — both go into the cluster Secret.
./pmm secret sk_test_xxxxxxxx pk_test_yyyyyy

# 3. Build image, load into minikube, apply manifests, restart pods
./pmm deploy

# 4. Open the web UI — pick ONE of the options below
```

### Open the web UI

**Option A — `./pmm ui` (easiest, one command):**

```sh
./pmm ui
```

This starts a background port-forward and opens `http://localhost:8000` in your
browser automatically. The forward keeps running after the command exits —
stop it later with `./pmm ui-stop`.

**Option B — port-forward manually (foreground):**

```sh
./pmm port-forward
```

Runs `kubectl port-forward` in the **foreground** — leave that terminal
running, then open `http://localhost:8000` in your browser yourself.
Press `Ctrl+C` to stop the forward (pods keep running).

**Option C — minikube service tunnel:**

```sh
./pmm tunnel
```

Prints a `http://192.168.x.x:30081` URL (the NodePort on the minikube VM's IP)
and can open it in your browser automatically.

Either way you should see the **paymemoney** page with the amount field and
the Payment / Refund / Withdrawal buttons.

Check everything is healthy:

```sh
./pmm status              # kubectl get pods,svc -n paymemoney
./pmm logs                # tail web pod logs
```

## Testing the flows

- **Payment** — enter an amount + currency → **Payment** → pick a method:
  - **QR Code** → get a scannable QR of a real Stripe Payment Link
  - **Credit Card** → Stripe.js Card Element (PCI-safe); test card `4242 4242 4242 4242`, any future exp, any cvc
  - **Bank Transfer** → routing `110000000`, any account number; expect `requires_action` (bank debits verify asynchronously)
  
  The result page shows a **request/response transcript** — for every Stripe
  call it renders `sent to Stripe` (exact form fields) and `Stripe responded`
  (raw JSON). Bank transfer shows both calls (PaymentMethod + PaymentIntent).
- **Refund** — copy the `pi_...` id from a payment result → **Refund** → paste it.
- **Withdrawal** — needs `STRIPE_TRANSFER_DESTINATION` set to a Stripe Connect
  destination account (`acct_...`, create one in the Stripe dashboard test mode):

  ```sh
  kubectl set env deployment/payment STRIPE_TRANSFER_DESTINATION=acct_xxx -n paymemoney
  ```

  Without it, the payment pod returns a friendly error by design.

Currency notes: the UI offers `usd` / `jpy` / `cny` (Chinese Yuan). Amounts are converted to Stripe's smallest unit (cents for usd/cny, whole yen for jpy).

## Secrets

`k8s/secret.yaml` is a **template only** — it is intentionally excluded from the
kustomization so `./pmm deploy` can never overwrite a real key with the
placeholder. Use `./pmm secret` instead (see above).

For production, do not commit secrets at all — use an external store:

- **External Secrets Operator** syncing from AWS/GCP/Azure secret managers
- **HashiCorp Vault** Agent Injector
- **Sealed Secrets** (Bitnami) if manifests must live in git
- Enable **envelope encryption at rest** for Secrets on the cluster
  (Kubernetes `EncryptionConfiguration` with a KMS provider)

Also plan to move from the full secret key to a **restricted Stripe key**
(dashboard → Developers → API keys → create restricted key) before prod.

## Unit tests

```sh
./pmm test
```

pytest + FastAPI `TestClient`, no cluster or Stripe key needed. `call_pay` and
`stripe_post` are monkeypatched, so tests never touch the network. Dev deps
live in `requirements-dev.txt` (kept out of the Docker image); the first run
creates `.venv` with Homebrew's python3.

- `tests/test_pay.py` — currency/amount conversion, Stripe form fields,
  refund/withdraw payloads, missing-key and missing-destination errors
- `tests/test_main.py` — every page renders, forms forward the right payload
  to the payment pod, QR image generation, error pages

## Useful commands

```sh
./pmm status          # pods + services
./pmm logs            # web pod logs (-f)
kubectl logs -n paymemoney -l app.kubernetes.io/name=payment --tail=100 -f  # payment pod
./pmm port-forward    # localhost:8000 -> web service (foreground)
./pmm ui              # background port-forward + auto-open browser
./pmm ui-stop         # stop the background port-forward
./pmm break-api ...   # move the API-level bug (see Debug modes)
./pmm break-k8s ...   # move the k8s-level bug (see Debug modes)
./pmm delete          # remove all k8s resources + namespace (cluster keeps running)
```

## Shutdown — turn everything off

```sh
./pmm delete          # 1. remove deployments/services/namespace
minikube stop        # 2. stop the cluster VM
# 3. quit Docker Desktop (optional)
```

Or do all three at once:

```sh
kubectl delete -k k8s && kubectl delete namespace paymemoney --ignore-not-found \
  && minikube stop
```

To start again later: `./setup.sh` → `./pmm secret ...` → `./pmm deploy` → `./pmm port-forward`.
(`minikube stop` preserves the VM disk, so redeploying after a restart is fast.)

## Project layout

```
app/
  main.py            # web pod: HTML pages + proxy to payment pod
  pay.py             # payment pod: Stripe REST calls (pay/card/bank/qr/refund/withdraw)
  __init__.py
k8s/
  namespace.yaml     # namespace: paymemoney
  configmap.yaml     # APP_NAME, PAYMENT_SERVICE_URL, STRIPE_PUBLISHABLE_KEY
  deployment.yaml    # web pod (paymemoney, 2 replicas, NodePort 30081)
  service.yaml       # web service
  payment-deployment.yaml  # payment pod (1 replica, same image)
  payment-service.yaml     # ClusterIP service "payment"
  debug-api-deployment.yaml   # debug pod: PAY_BREAK env picks the API bug
  debug-api-service.yaml      # healthy service -> debug-api pod
  debug-k8s-deployment.yaml   # healthy pod (label debug-k8s)
  debug-k8s-service.yaml      # correct baseline; break-k8s.sh breaks it live
  secret.yaml        # Secret TEMPLATE (not applied; use `./pmm secret`)
  kustomization.yaml
Dockerfile           # one image for all pods
break-k8s.sh         # introduces/heals k8s-level bugs on debug-k8s (live only)
pmm                  # CLI: deploy/secret/test/ui/break-api/break-k8s/...
setup.sh             # prereq check + install/upgrade + minikube start
requirements.txt     # runtime deps (fastapi, uvicorn, httpx, segno, python-multipart)
requirements-dev.txt # pytest — local tests only, not in the image
tests/               # unit tests (pytest)
```

## Known v1 simplifications

- Card payments keep cardholder data out of our infrastructure entirely:
  Stripe.js tokenizes in the browser (publishable key), the pod only sees a
  `pm_...` id. Remaining PCI-adjacent gaps for production: use the Payment
  Element (richer UX, handles 3DS/SCA automatically), add Stripe webhooks for
  async confirmation, and complete a formal PCI SAQ-A assessment.
- QR payment creates a real Stripe Payment Link each time (no expiry/cancel
  management yet — links accumulate in the Stripe dashboard).
- State is in-memory per pod — fine for learning, not for real traffic.
