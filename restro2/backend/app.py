import os
import hmac
import hashlib
import json
import urllib.request
from urllib.parse import quote
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
# Enable CORS for all routes to allow frontend requests
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Razorpay Configuration — keys MUST come from backend/.env (never hardcode real secrets)
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
# Webhook signing secret (Razorpay Dashboard -> Webhooks -> Secret).
# Enables /api/webhook/razorpay, which settles an order even if the customer's
# phone closes the checkout before the client-side verify call runs.
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
# Firestore project id. Lets the backend recompute live order totals through the
# public Firestore REST API (read-only) even when the Admin SDK service account
# is not configured — so payment amounts can be validated against real orders.
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()

# Simulation mode is OFF unless explicitly enabled with PAYMENT_SIMULATION=true in .env.
# NEVER enable in production: it accepts payments without a real gateway.
PAYMENT_SIMULATION = os.getenv("PAYMENT_SIMULATION", "false").strip().lower() in ("1", "true", "yes")

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Initialize Razorpay Client if credentials are provided
razorpay_client = None
try:
    import razorpay
    if RAZORPAY_KEY_ID and not RAZORPAY_KEY_ID.startswith("rzp_test_YOUR"):
        razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        print("[Razorpay] Client initialized with configured keys.")
    else:
        print("[Warning] RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not configured in backend/.env")
except Exception as e:
    print(f"[Warning] Razorpay client init skipped: {e}")

# Initialize Stripe Client if credentials are provided
if STRIPE_SECRET_KEY:
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
    except Exception as e:
        print(f"[Warning] Stripe client init skipped: {e}")

# Optional: Firebase Admin SDK for direct server-side Firestore updates
firebase_db = None
service_account_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "")
if service_account_path and os.path.exists(service_account_path):
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        cred = credentials.Certificate(service_account_path)
        firebase_admin.initialize_app(cred)
        firebase_db = firestore.client()
        print("[Firebase] Admin SDK initialized successfully.")
    except Exception as e:
        print(f"[Firebase] Admin SDK init failed: {e}")


# ---------------------------------------------------------------------------
# Payment-key mode + server-side order amount validation
# ---------------------------------------------------------------------------
def _key_mode():
    """'live' | 'test' | 'unset' | 'unknown' — decoded from the key prefix."""
    kid = (RAZORPAY_KEY_ID or "").lower()
    if not kid:
        return "unset"
    if kid.startswith("rzp_live_"):
        return "live"
    if kid.startswith("rzp_test_"):
        return "test"
    return "unknown"


IS_TEST_MODE = _key_mode() == "test"
# Amount validation is possible only when we can read real order documents.
ORDER_VALIDATION_AVAILABLE = bool(firebase_db) or bool(FIREBASE_PROJECT_ID)
# ₹1 slack absorbs client/server GST rounding differences.
ORDER_AMOUNT_TOLERANCE_PAISE = 100


def _fs_rest_value(v):
    """Convert one Firestore REST API value into a plain Python value."""
    if not isinstance(v, dict):
        return None
    if "stringValue" in v:
        return v["stringValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "booleanValue" in v:
        return bool(v["booleanValue"])
    if "nullValue" in v:
        return None
    if "arrayValue" in v:
        return [_fs_rest_value(x) for x in (v["arrayValue"].get("values") or [])]
    if "mapValue" in v:
        return {k: _fs_rest_value(x) for k, x in (v["mapValue"].get("fields") or {}).items()}
    return None


def _fetch_order(order_id):
    """Read one order doc — Admin SDK first, else the public Firestore REST API."""
    if firebase_db is not None:
        try:
            snap = (firebase_db.collection("restaurants").document("main")
                    .collection("orders").document(str(order_id)).get())
            return snap.to_dict() if snap.exists else None
        except Exception as e:
            print(f"[Orders] Admin read failed for {order_id}: {e}")
            return None
    if FIREBASE_PROJECT_ID:
        url = ("https://firestore.googleapis.com/v1/projects/"
               f"{FIREBASE_PROJECT_ID}/databases/(default)/documents/"
               f"restaurants/main/orders/{quote(str(order_id), safe='')}")
        try:
            with urllib.request.urlopen(url, timeout=6) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            return {k: _fs_rest_value(v) for k, v in (raw.get("fields") or {}).items()}
        except Exception as e:
            print(f"[Orders] REST read failed for {order_id}: {e}")
            return None
    return None


def _order_total_inr(order):
    """Recompute an order's payable total (subtotal + GST) from its stored items."""
    total = 0.0
    for i in (order.get("items") or []):
        try:
            line = float(i.get("qty") or 0) * float(i.get("price") or 0)
            rate = float(i.get("gst") or 0)
        except (TypeError, ValueError):
            continue
        total += line + round(line * rate / 100)
    return total


def _expected_amount_inr(order_ids):
    """Sum the real totals of the given unpaid orders.

    Returns (expected_inr, resolved_count). resolved_count == 0 means no order
    could be read, so amount validation is skipped (logged loudly).
    """
    if not ORDER_VALIDATION_AVAILABLE:
        return 0.0, 0
    expected = 0.0
    resolved = 0
    for oid in (order_ids or []):
        doc = _fetch_order(oid)
        if doc is None:
            continue
        resolved += 1
        if str(doc.get("payment") or "").lower() == "paid":
            continue  # already settled — not part of this payment
        expected += _order_total_inr(doc)
    return expected, resolved


@app.route("/api/health", methods=["GET"])
def health_check():
    """Health check endpoint to verify backend status."""
    return jsonify({
        "status": "healthy",
        "service": "MenuQR Payment Gateway Backend",
        "razorpay_configured": bool(razorpay_client),
        "stripe_configured": bool(STRIPE_SECRET_KEY),
        "firebase_admin_configured": bool(firebase_db),
        "key_mode": _key_mode(),
        "test_mode": IS_TEST_MODE,
        "order_validation": ORDER_VALIDATION_AVAILABLE,
        "webhook_configured": bool(RAZORPAY_WEBHOOK_SECRET)
    }), 200


@app.route("/api/config", methods=["GET"])
def get_public_config():
    """Returns public keys required by frontend."""
    return jsonify({
        "razorpay_key_id": RAZORPAY_KEY_ID,
        "stripe_publishable_key": STRIPE_PUBLISHABLE_KEY,
        "mode": "live" if razorpay_client else "simulation",
        "key_mode": _key_mode(),
        "test_mode": IS_TEST_MODE
    }), 200


@app.route("/api/create-order", methods=["POST"])
def create_payment_order():
    """
    Creates a payment gateway order (Razorpay / Gateway).
    Expected JSON payload:
    {
        "amount": 250,          // Amount in INR (Rupees)
        "currency": "INR",
        "receipt": "order_1001",
        "orderIds": ["1001"],
        "customer": {"name": "Rahul", "phone": "9876543210"},
        "table": "Table 1"
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        try:
            amount_inr = float(data.get("amount", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid amount. Must be a number."}), 400
        currency = str(data.get("currency", "INR")).upper()
        receipt = str(data.get("receipt") or f"rcpt_{os.urandom(4).hex()}")[:40]
        order_ids = data.get("orderIds", [])
        table = str(data.get("table", "Table 1"))[:40]

        amount_paise = int(round(amount_inr * 100))  # Razorpay expects amount in paise

        # Razorpay requires a minimum of 100 paise (₹1)
        if amount_paise < 100:
            return jsonify({"error": "Invalid amount. Minimum is 100 paise (₹1)."}), 400

        # Bind the payment to the real orders: recompute their total server-side
        # and reject any amount that does not cover it (blocks pay-₹1-for-₹1000).
        expected_inr, resolved = _expected_amount_inr(order_ids)
        if resolved > 0 and amount_paise + ORDER_AMOUNT_TOLERANCE_PAISE < int(round(expected_inr * 100)):
            print(f"[Security] Underpayment blocked: claimed Rs.{amount_inr}, orders total Rs.{expected_inr} ({resolved} order(s) checked)")
            return jsonify({"error": "Amount does not cover your order. Please refresh the bill and try again."}), 400
        if resolved == 0 and ORDER_VALIDATION_AVAILABLE and order_ids:
            print(f"[Security] Warning: could not read any of {len(order_ids)} order(s) to validate the amount.")

        # Gateway must be configured (or explicit simulation mode) — fail loudly, never fake an order
        if not razorpay_client and not PAYMENT_SIMULATION:
            return jsonify({"error": "Payment gateway not configured on server. Add Razorpay keys to backend/.env."}), 503

        # 1. If Razorpay client is active with real keys
        if razorpay_client:
            order_data = {
                "amount": amount_paise,
                "currency": currency,
                "receipt": receipt,
                "notes": {
                    "table": table,
                    "orderIds": ",".join(map(str, order_ids)),
                    "amountInr": str(amount_inr)
                }
            }
            rzp_order = razorpay_client.order.create(data=order_data)
            return jsonify({
                "success": True,
                "provider": "razorpay",
                "order_id": rzp_order["id"],
                "amount": rzp_order["amount"],
                "currency": rzp_order["currency"],
                "key_id": RAZORPAY_KEY_ID
            }), 200

        # 2. Simulation / Development Mode fallback — only reachable when PAYMENT_SIMULATION=true
        simulated_order_id = f"order_sim_{os.urandom(8).hex()}"
        print(f"[Simulation] Created mock payment order: {simulated_order_id} for Rs.{amount_inr}")
        return jsonify({
            "success": True,
            "provider": "simulation",
            "order_id": simulated_order_id,
            "amount": amount_paise,
            "currency": currency,
            "key_id": RAZORPAY_KEY_ID,
            "message": "Running in test simulation mode. Configure .env with live keys when ready."
        }), 200

    except Exception as e:
        err = getattr(e, "args", [None])[0]
        status = err.get("status_code") if isinstance(err, dict) else None
        status = status or getattr(e, "status_code", None)
        if status == 401:
            print(f"[Error] Razorpay authentication failed: {e}")
            return jsonify({"error": "Razorpay authentication failed. Check API keys."}), 401
        print(f"[Error] create_payment_order failed: {e}")
        return jsonify({"error": "Payment gateway error. Please try again."}), 500


@app.route("/api/verify-payment", methods=["POST"])
def verify_payment():
    """
    Verifies the payment signature returned by the gateway.
    Expected JSON payload:
    {
        "razorpay_order_id": "order_xxx",
        "razorpay_payment_id": "pay_xxx",
        "razorpay_signature": "sig_xxx",
        "orderIds": ["1001", "1002"],
        "is_simulation": false
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        order_id = data.get("razorpay_order_id")
        payment_id = data.get("razorpay_payment_id")
        signature = data.get("razorpay_signature")
        order_ids = data.get("orderIds", [])

        # Missing fields → 400 (never mark as paid)
        if not order_id or not payment_id or not signature:
            return jsonify({"success": False, "error": "Missing fields: razorpay_order_id, razorpay_payment_id and razorpay_signature are required."}), 400

        if not razorpay_client and not PAYMENT_SIMULATION:
            return jsonify({"success": False, "error": "Payment gateway not configured on server."}), 503

        is_verified = False

        if PAYMENT_SIMULATION and not razorpay_client:
            # Explicit development simulation only (PAYMENT_SIMULATION=true in .env)
            is_verified = True
            print(f"[Simulation] Payment accepted: {payment_id}")
        else:
            # Cryptographic HMAC-SHA256 verification: HMAC(order_id + "|" + payment_id, KEY_SECRET)
            generated_signature = hmac.new(
                RAZORPAY_KEY_SECRET.encode("utf-8"),
                f"{order_id}|{payment_id}".encode("utf-8"),
                hashlib.sha256
            ).hexdigest()

            if not isinstance(signature, str) or not hmac.compare_digest(generated_signature, signature):
                # Signature mismatch → 400, do NOT mark as paid
                print(f"[Razorpay] Signature verification FAILED for payment {payment_id}")
                return jsonify({"success": False, "error": "Signature verification failed"}), 400
            is_verified = True
            print(f"[Razorpay] Payment signature verified: {payment_id}")

        # Second gate: confirm the amount Razorpay actually captured covers the
        # real orders, so a valid signature on a tiny payment can't settle a big bill.
        if is_verified and razorpay_client and ORDER_VALIDATION_AVAILABLE and order_ids:
            try:
                pay = razorpay_client.payment.fetch(payment_id)
                paid_paise = int(pay.get("amount") or 0)
                expected_inr, resolved = _expected_amount_inr(order_ids)
                if resolved > 0 and paid_paise + ORDER_AMOUNT_TOLERANCE_PAISE < int(round(expected_inr * 100)):
                    print(f"[Security] verify-payment underpayment: paid {paid_paise}p, orders total Rs.{expected_inr}")
                    return jsonify({"success": False, "error": "Payment amount does not cover the orders."}), 400
            except Exception as e:
                print(f"[Security] Amount cross-check skipped: {e}")

        # If Firebase Admin SDK is configured, update Firestore server-side securely
        if is_verified and firebase_db and order_ids:
            try:
                for oid in order_ids:
                    doc_ref = firebase_db.collection("restaurants").document("main").collection("orders").document(str(oid))
                    doc_ref.update({
                        "payment": "paid",
                        "payMethod": "online",
                        "paymentId": payment_id,
                        "paidAt": firestore.SERVER_TIMESTAMP
                    })
                print(f"[Firebase] Updated {len(order_ids)} orders to 'paid' in Firestore.")
            except Exception as fe:
                print(f"[Firebase] Server-side update warning: {fe}")

        return jsonify({
            "success": True,
            "verified": is_verified,
            "payment_id": payment_id,
            "message": "Payment verified and recorded successfully."
        }), 200

    except Exception as e:
        print(f"[Error] verify_payment failed: {e}")
        return jsonify({"error": "Payment verification error. Please try again."}), 500


@app.route("/api/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    """Server-to-server payment confirmation.

    Razorpay calls this even if the customer's browser closes before the
    client-side verify runs. The raw body is authenticated with the webhook
    secret (X-Razorpay-Signature), so only genuine Razorpay events are trusted.
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        return jsonify({"error": "Webhook secret not configured (set RAZORPAY_WEBHOOK_SECRET)."}), 503

    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected_sig = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected_sig, signature):
        print("[Webhook] Rejected: invalid signature")
        return jsonify({"error": "Invalid signature"}), 400

    event = request.get_json(silent=True) or {}
    event_name = event.get("event", "")
    if event_name not in ("payment.captured", "order.paid"):
        return jsonify({"status": "ignored", "event": event_name}), 200

    entity = (((event.get("payload") or {}).get("payment") or {}).get("entity") or {})
    payment_id = entity.get("id")
    rzp_order_id = entity.get("order_id")
    paid_paise = int(entity.get("amount") or 0)

    order_ids = []
    if razorpay_client and rzp_order_id:
        try:
            rzp_order = razorpay_client.order.fetch(rzp_order_id)
            order_ids = [x for x in (rzp_order.get("notes", {}).get("orderIds") or "").split(",") if x]
        except Exception as e:
            print(f"[Webhook] Could not fetch order {rzp_order_id}: {e}")

    if not firebase_db or not order_ids:
        # No server-side write path (Admin SDK absent) or no orders referenced.
        return jsonify({"status": "ok", "updated": 0, "note": "no server-side update path"}), 200

    expected_inr, resolved = _expected_amount_inr(order_ids)
    if resolved > 0 and paid_paise + ORDER_AMOUNT_TOLERANCE_PAISE < int(round(expected_inr * 100)):
        print(f"[Webhook] Amount mismatch for {rzp_order_id}: paid {paid_paise}p, orders total Rs.{expected_inr}")
        return jsonify({"status": "amount_mismatch"}), 400

    updated = 0
    for oid in order_ids:
        try:
            (firebase_db.collection("restaurants").document("main")
             .collection("orders").document(str(oid)).update({
                 "payment": "paid",
                 "payMethod": "online",
                 "paymentId": payment_id,
                 "paidAt": firestore.SERVER_TIMESTAMP
             }))
            updated += 1
        except Exception as e:
            print(f"[Webhook] Update failed for order {oid}: {e}")
    print(f"[Webhook] {event_name}: marked {updated} order(s) paid (payment {payment_id})")
    return jsonify({"status": "ok", "updated": updated}), 200


# ---------------------------------------------------------------------------
# Serve the restaurant app itself (single-file frontend) from this backend.
# Open http://127.0.0.1:5000 on this PC — or http://<this-PC-IP>:5000 on any
# phone connected to the same Wi-Fi — and online payments work same-origin.
# ---------------------------------------------------------------------------
FRONTEND_FILE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index (1).html"))


@app.route("/", methods=["GET"])
def serve_frontend():
    """Serve the MenuQR app so the whole product runs from one URL."""
    if os.path.exists(FRONTEND_FILE):
        return send_file(FRONTEND_FILE)
    return jsonify({
        "service": "MenuQR Payment Gateway Backend",
        "hint": "index (1).html not found in the parent folder; place the frontend next to backend/."
    }), 404


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    return ("", 204)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").strip().lower() in ("1", "true", "yes")
    if IS_TEST_MODE:
        print("[Warning] ============================================")
        print("[Warning] RAZORPAY TEST KEYS in use (rzp_test_...).")
        print("[Warning] No real money is collected. Do NOT use in production.")
        print("[Warning] ============================================")
    if not ORDER_VALIDATION_AVAILABLE:
        print("[Warning] Order-amount validation OFF: set FIREBASE_PROJECT_ID or")
        print("[Warning] FIREBASE_SERVICE_ACCOUNT_PATH to validate payments against real orders.")
    print(f"MenuQR Payment Gateway Backend running on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
