# MenuQR Payment Backend (Python)

A small Flask microservice that handles secure online payments for MenuQR via **Razorpay Standard Checkout**.
The secret key lives ONLY here — the frontend never touches `RAZORPAY_KEY_SECRET`.

## What it does
- **`POST /api/create-order`** → creates a Razorpay order (amount converted to paise; minimum ₹1 / 100 paise) and returns `{ order_id, amount, currency, key_id }`.
- **`POST /api/verify-payment`** → verifies the checkout signature with **HMAC-SHA256** (`order_id|payment_id`, secret key). Mismatch or missing fields → **400**, order is never marked paid. It also cross-checks that the amount Razorpay actually captured covers the real orders.
- **`POST /api/webhook/razorpay`** → server-to-server confirmation (signature-verified with `RAZORPAY_WEBHOOK_SECRET`). Settles an order even if the customer's phone closes the checkout before the client-side verify runs.
- **`GET /api/health`**, **`GET /api/config`** → status/public key, plus `key_mode`/`test_mode` so test keys can never be mistaken for live.
- Optional server-side Firestore update after verification (needs `FIREBASE_SERVICE_ACCOUNT_PATH`; not required for local use).

### Amount validation (pay-₹1-for-₹1000 protection)
On top of the signature check, the backend recomputes each order's real total (**subtotal + GST**)
from its stored items and **rejects any payment that does not cover it** — in `create-order`,
in `verify-payment`, and in the webhook. It reads orders via the **Firebase Admin SDK** if
configured, otherwise through the **public Firestore REST API** (read-only) using `FIREBASE_PROJECT_ID`.
Set either one in production, or the backend logs a loud warning and skips the amount check
(`GET /api/health` → `"order_validation": true` once it is active).

## Setup & Run (easiest: one click)

Double-click **`backend\start_backend.bat`** (or run it from a terminal).
It creates the virtual environment, installs dependencies on first run, and starts the server on
`http://127.0.0.1:5000`. Keep that window open while taking online payments.

The backend also **serves the restaurant app itself**, so one URL does everything:
- On this PC → open `http://127.0.0.1:5000`
- On any phone on the same Wi-Fi → open `http://<PC-IP>:5000` (the IP is printed on startup,
  e.g. `http://192.168.0.194:5000`). Payments go through this PC — no deployment needed for
  in-restaurant testing.

### Manual setup (same thing by hand)

```bat
cd backend
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

Copy `.env.example` to `.env` and put your keys in:

```
RAZORPAY_KEY_ID=rzp_test_xxx
RAZORPAY_KEY_SECRET=xxx
RAZORPAY_WEBHOOK_SECRET=xxx      # optional: enables /api/webhook/razorpay
FIREBASE_PROJECT_ID=restro-qr-fd790   # enables server-side amount validation
PAYMENT_SIMULATION=false
```

`.env` is git-ignored — never commit it.

### Troubleshooting

- `ModuleNotFoundError: No module named 'flask'` → you ran the **system** Python instead of the
  project venv. Use `start_backend.bat`, or in VS Code select the interpreter
  `backend\.venv\Scripts\python.exe` (Ctrl+Shift+P → “Python: Select Interpreter”).
- Port already in use → change `PORT` in `.env` and update `PAYMENT_BACKEND_URL` in `index (1).html`
  to match.

Then open the restaurant app (`index (1).html`) and use the **Pay** section on the bill screen
(**Pay by Card / NetBanking**, **Pay by App**, **Pay Cash**).

## Test payments (test mode keys)
Use any test method shown in the Razorpay modal, e.g.:
- Card `4111 1111 1111 1111`, any future expiry, any CVV (test mode)
- Or NetBanking

### UPI app payments (PhonePe / Google Pay / Paytm) DO NOT work on test keys
Razorpay's own docs (Test Integration) state: *"UPI payments should be tested in Live Mode."*
The UPI **app / intent** flow — the one that lists your installed UPI apps — is unavailable with
`rzp_test_...` keys, and UPI Collect is not supported on Android per NPCI rules. With test keys a
UPI-only checkout therefore fails with *"No appropriate payment method found"*.

The app handles this automatically: `liveKeysInUse()` checks the key prefix from `/api/create-order`,
and **Pay by App** only locks Razorpay to the UPI-only app list when live keys (`rzp_live_...`) are in
use on a phone browser. With test keys (or on desktop / inside WhatsApp & Instagram) it keeps cards +
netbanking as a fallback so the customer is never stuck, and the app list shows a "Test mode chal raha
hai" notice. Swap in live keys and the app list activates with no code change.

On success the modal handler calls `/api/verify-payment`; only a **verified signature** marks the
orders `payment:'paid'` in Firestore (fields `payment`/`payMethod` are whitelisted by the rules).

## Security notes
- **Test keys are safe to keep for testing** — `rzp_test_...` can never move real money. But on a
  deployed site they would let real customers "pay" while you collect ₹0, so never ship test keys.
  The backend logs a `[Warning] RAZORPAY TEST KEYS` banner on startup and reports `"test_mode": true`
  from `/api/health`, and the app shows a TEST MODE badge.
- Without `FIREBASE_PROJECT_ID` / `FIREBASE_SERVICE_ACCOUNT_PATH`, the backend cannot see real order
  totals, so the amount check is **off**. Set one in production.
- `PAYMENT_SIMULATION=true` accepts payments WITHOUT a gateway — dev only, never in production.
- For production, deploy this service (Render/Railway/VPS) and set
  `PAYMENT_BACKEND_URL` in `index (1).html` to your server URL. Use `gunicorn app:app` instead of
  the Flask dev server, and serve the frontend over HTTPS (Razorpay requires HTTPS in live mode).
