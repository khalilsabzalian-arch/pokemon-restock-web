#!/usr/bin/env python3
"""
Pokemon In-Store Restock Tracker — web app.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template, request

import checkers

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PRODUCTS_FILE = os.path.join(DATA_DIR, "products.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "bestbuy_api_key": "",
    "twilio_account_sid": "",
    "twilio_auth_token": "",
    "twilio_from_number": "",
    "twilio_to_number": "",
    "check_interval_seconds": 120,
}

app = Flask(__name__)
lock = threading.Lock()


def _load(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def _save(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_products():
    return _load(PRODUCTS_FILE, [])


def save_products(products):
    _save(PRODUCTS_FILE, products)


def load_state():
    return _load(STATE_FILE, {})


def save_state(state):
    _save(STATE_FILE, state)


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    settings.update(_load(SETTINGS_FILE, {}))
    return settings


def save_settings(settings):
    _save(SETTINGS_FILE, settings)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def send_sms(settings: dict, body: str) -> None:
    sid = settings.get("twilio_account_sid")
    token = settings.get("twilio_auth_token")
    from_number = settings.get("twilio_from_number")
    to_number = settings.get("twilio_to_number")

    if not all([sid, token, from_number, to_number]):
        log(f"Twilio not configured — would have texted: {body}")
        return

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        resp = requests.post(
            url,
            data={"From": from_number, "To": to_number, "Body": body},
            auth=(sid, token),
            timeout=15,
        )
        if resp.status_code >= 300:
            log(f"Twilio error {resp.status_code}: {resp.text}")
        else:
            log(f"Texted: {body}")
    except requests.RequestException as e:
        log(f"Failed to send SMS: {e}")


def run_checks_once():
    with lock:
        products = load_products()
        state = load_state()
        settings = load_settings()

    for product in products:
        pid = product["id"]
        try:
            results = checkers.check_product(product, settings.get("bestbuy_api_key", ""))
            now_in_stock = any(r["in_stock"] for r in results)
            error = None
        except Exception as e:
            results = []
            now_in_stock = state.get(pid, {}).get("in_stock", False)
            error = str(e)
            log(f"Error checking {product.get('name', pid)}: {e}")

        prev = state.get(pid, {})
        was_in_stock = prev.get("in_stock", False)

        if now_in_stock and not was_in_stock:
            stores = [r["store_name"] for r in results if r["in_stock"]]
            url = results[0]["url"] if results else ""
            send_sms(settings, f"IN STOCK: {product['name']} at {', '.join(stores)} — {url}")

        state[pid] = {
            "in_stock": now_in_stock,
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "stores": results,
            "error": error,
        }

    with lock:
        save_state(state)


def background_loop():
    while True:
        settings = load_settings()
        interval = int(settings.get("check_interval_seconds", 120) or 120)
        try:
            run_checks_once()
        except Exception as e:
            log(f"Background loop error: {e}")
        time.sleep(max(30, interval))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/products", methods=["GET"])
def api_get_products():
    with lock:
        products = load_products()
        state = load_state()
    merged = []
    for p in products:
        merged.append({**p, "status": state.get(p["id"], {})})
    return jsonify(merged)


@app.route("/api/products", methods=["POST"])
def api_add_product():
    body = request.get_json(force=True)
    required_common = ["name", "retailer", "zip"]
    if not all(body.get(k) for k in required_common):
        return jsonify({"error": "name, retailer, and zip are required"}), 400

    retailer = body["retailer"]
    id_fields = {
        "bestbuy": ["sku"],
        "target": ["tcin", "store_id"],
        "walmart": ["item_id"],
        "walgreens": ["product_id", "store_id"],
    }
    if retailer not in id_fields:
        return jsonify({"error": f"Unsupported retailer '{retailer}'"}), 400
    if not all(body.get(f) for f in id_fields[retailer]):
        return jsonify({"error": f"{retailer} needs: {', '.join(id_fields[retailer])}"}), 400

    product = {"id": str(uuid.uuid4()), **body}
    with lock:
        products = load_products()
        products.append(product)
        save_products(products)

    threading.Thread(target=run_checks_once, daemon=True).start()
    return jsonify(product), 201


@app.route("/api/products/<pid>", methods=["DELETE"])
def api_delete_product(pid):
    with lock:
        products = [p for p in load_products() if p["id"] != pid]
        save_products(products)
        state = load_state()
        state.pop(pid, None)
        save_state(state)
    return jsonify({"deleted": pid})


@app.route("/api/check-now", methods=["POST"])
def api_check_now():
    threading.Thread(target=run_checks_once, daemon=True).start()
    return jsonify({"status": "checking"})


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    settings = load_settings()
    redacted = {
        "bestbuy_api_key_set": bool(settings.get("bestbuy_api_key")),
        "twilio_configured": bool(
            settings.get("twilio_account_sid") and settings.get("twilio_auth_token")
            and settings.get("twilio_from_number") and settings.get("twilio_to_number")
        ),
        "twilio_to_number": settings.get("twilio_to_number", ""),
        "check_interval_seconds": settings.get("check_interval_seconds", 120),
    }
    return jsonify(redacted)


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    body = request.get_json(force=True)
    with lock:
        settings = load_settings()
        for key in DEFAULT_SETTINGS:
            if key in body and body[key] != "":
                settings[key] = body[key]
        save_settings(settings)
    return jsonify({"saved": True})


if __name__ == "__main__":
    threading.Thread(target=background_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
