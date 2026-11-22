import csv
import io
import os
import re
import secrets
import sqlite3
import time
import razorpay
from dotenv import load_dotenv
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, Response, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parking.db")

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Admin@12345")
SENSOR_KEY = os.environ.get("SENSOR_KEY", "sensor-dev-key")

ZONES = ("General", "EV Charging", "Disabled", "VIP")
MAX_SLOTS = 100

SETTING_DEFAULTS = {
    "rate_per_hour": "20",
    "max_hours": "12",
    "hold_minutes": "5",
    "lot_name": "ParkWise Parking Lot",
}

PLATE_PATTERN = re.compile(r"^[A-Z]{2}\d{2}[A-Z]{1,3}\d{4}$")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,20}$")
UPI_PATTERN = re.compile(r"^[A-Za-z0-9.\-_]{2,}@[A-Za-z]{2,}$")
DECLINED_CARD = "4000000000000002"  # test card that is always declined


# ==========================================
# DATABASE
# ==========================================

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def add_column_if_missing(db, table, column, definition):
    cols = [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL
            )
        """)
        add_column_if_missing(db, "users", "role", "TEXT NOT NULL DEFAULT 'user'")
        add_column_if_missing(db, "users", "blocked", "INTEGER NOT NULL DEFAULT 0")
        add_column_if_missing(db, "users", "created_at", "INTEGER")

        db.execute("""
            CREATE TABLE IF NOT EXISTS slots (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'available',
                user_id INTEGER,
                vehicle TEXT,
                start_time INTEGER,
                end_time INTEGER,
                price INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        for col in ("start_time", "end_time", "price"):
            add_column_if_missing(db, "slots", col, "INTEGER")
        add_column_if_missing(db, "slots", "zone", "TEXT NOT NULL DEFAULT 'General'")

        db.execute("""
            CREATE TABLE IF NOT EXISTS reservations_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                slot TEXT,
                vehicle TEXT,
                start_time INTEGER,
                end_time INTEGER,
                price INTEGER,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        add_column_if_missing(db, "reservations_log", "hours", "INTEGER")
        add_column_if_missing(db, "reservations_log", "payment_status", "TEXT NOT NULL DEFAULT 'none'")
        add_column_if_missing(db, "reservations_log", "payment_method", "TEXT")
        add_column_if_missing(db, "reservations_log", "txn_id", "TEXT")
        add_column_if_missing(db, "reservations_log", "paid_at", "INTEGER")
        add_column_if_missing(db, "reservations_log", "hold_expires", "INTEGER")

        db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reservation_id INTEGER,
                user_id INTEGER,
                amount INTEGER NOT NULL,
                method TEXT,
                detail TEXT,
                status TEXT NOT NULL,
                txn_id TEXT,
                created_at INTEGER
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        for key, value in SETTING_DEFAULTS.items():
            db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )

        db.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                message TEXT NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0
            )
        """)

        admin = db.execute(
            "SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)
        ).fetchone()
        if admin:
            db.execute(
                "UPDATE users SET password_hash = ?, role = 'admin', blocked = 0 WHERE username = ?",
                (generate_password_hash(ADMIN_PASSWORD), ADMIN_USERNAME),
            )
        else:
            db.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
                (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD), int(time.time())),
            )

        count = db.execute("SELECT COUNT(*) FROM slots").fetchone()[0]
        if count == 0:
            for i in range(1, 11):
                status = "occupied" if i in (2, 7) else "available"
                db.execute("INSERT INTO slots (id, status) VALUES (?, ?)", (f"P{i}", status))
        db.commit()


# Runs under gunicorn / PythonAnywhere too, not only "python app.py"
init_db()


# ==========================================
# HELPERS
# ==========================================

def get_settings(db):
    values = dict(SETTING_DEFAULTS)
    for r in db.execute("SELECT key, value FROM settings").fetchall():
        values[r["key"]] = r["value"]
    return {
        "rate": int(values["rate_per_hour"]),
        "max_hours": int(values["max_hours"]),
        "hold_seconds": int(values["hold_minutes"]) * 60,
        "lot_name": values["lot_name"],
    }


def notify(db, level, message, dedupe_seconds=0):
    now = int(time.time())
    if dedupe_seconds:
        dup = db.execute(
            "SELECT 1 FROM notifications WHERE message = ? AND created_at > ? LIMIT 1",
            (message, now - dedupe_seconds),
        ).fetchone()
        if dup:
            return
    db.execute(
        "INSERT INTO notifications (created_at, level, message, is_read) VALUES (?, ?, ?, 0)",
        (now, level, message),
    )
    # keep only the latest 200 notifications
    db.execute("""
        DELETE FROM notifications
        WHERE id NOT IN (SELECT id FROM notifications ORDER BY id DESC LIMIT 200)
    """)


def check_lot_full(db):
    free = db.execute(
        "SELECT COUNT(*) FROM slots WHERE status = 'available'"
    ).fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM slots").fetchone()[0]
    if total > 0 and free == 0:
        notify(db, "warning", "Parking lot is full: no slots are available.", 600)


def new_txn():
    return "TXN" + secrets.token_hex(5).upper()


def record_payment(db, reservation_id, user_id, amount, method, detail, status, txn_id):
    db.execute("""
        INSERT INTO payments
        (reservation_id, user_id, amount, method, detail, status, txn_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (reservation_id, user_id, amount, method, detail, status, txn_id, int(time.time())))


def valid_expiry(text):
    m = re.fullmatch(r"(\d{2})/(\d{2})", text)
    if not m:
        return False
    month, year = int(m.group(1)), 2000 + int(m.group(2))
    if not 1 <= month <= 12:
        return False
    today = datetime.now()
    return (year, month) >= (today.year, today.month)


def receipt_dict(db, r):
    pay_row = db.execute("""
        SELECT detail FROM payments
        WHERE reservation_id = ? AND status IN ('success', 'refunded')
        ORDER BY id DESC LIMIT 1
    """, (r["id"],)).fetchone()
    return {
        "receipt_no": "PW-%06d" % r["id"],
        "reservation_id": r["id"],
        "txn_id": r["txn_id"],
        "username": r["username"],
        "slot": r["slot"],
        "vehicle": r["vehicle"],
        "hours": r["hours"],
        "amount": r["price"],
        "method": r["payment_method"],
        "detail": pay_row["detail"] if pay_row else None,
        "payment_status": r["payment_status"],
        "paid_at_ms": r["paid_at"] * 1000 if r["paid_at"] else None,
        "start_ms": r["start_time"] * 1000 if r["start_time"] else None,
        "end_ms": r["end_time"] * 1000 if r["end_time"] else None,
    }


def release_expired(db):
    now = int(time.time())

    # 1. Unpaid bookings whose hold ran out: free the slot, mark expired
    stale = db.execute("""
        SELECT id, slot, user_id, start_time FROM reservations_log
        WHERE status = 'pending' AND hold_expires <= ?
    """, (now,)).fetchall()
    for r in stale:
        db.execute("""
            UPDATE slots
            SET status = 'available', user_id = NULL, vehicle = NULL,
                start_time = NULL, end_time = NULL, price = NULL
            WHERE id = ? AND user_id = ? AND status = 'reserved' AND start_time = ?
        """, (r["slot"], r["user_id"], r["start_time"]))
        db.execute("""
            UPDATE reservations_log
            SET status = 'expired', payment_status = 'failed'
            WHERE id = ?
        """, (r["id"],))
        notify(db, "info",
               f"Payment hold expired for slot {r['slot']} (booking #{r['id']}). Slot released.")

    # 2. Paid bookings whose parking time ended
    db.execute("""
        UPDATE reservations_log SET status = 'completed'
        WHERE status = 'active' AND end_time <= ?
    """, (now,))

    # 3. Free any reserved slot whose time ended
    db.execute("""
        UPDATE slots
        SET status = 'available', user_id = NULL, vehicle = NULL,
            start_time = NULL, end_time = NULL, price = NULL
        WHERE status = 'reserved' AND (end_time IS NULL OR end_time <= ?)
    """, (now,))


def free_slot(db, slot_id, user_id):
    db.execute("""
        UPDATE slots
        SET status = 'available', user_id = NULL, vehicle = NULL,
            start_time = NULL, end_time = NULL, price = NULL
        WHERE id = ? AND user_id = ? AND status = 'reserved'
    """, (slot_id, user_id))


def cancel_reservation_row(db, res, by):
    """Cancel a pending/active booking: free the slot and refund if it was paid."""
    free_slot(db, res["slot"], res["user_id"])

    refunded = res["payment_status"] == "paid"
    if refunded:
        db.execute("""
            UPDATE payments SET status = 'refunded'
            WHERE reservation_id = ? AND status = 'success'
        """, (res["id"],))

    new_pay = {"paid": "refunded", "pending": "failed"}.get(
        res["payment_status"], res["payment_status"]
    )
    db.execute("""
        UPDATE reservations_log SET status = 'cancelled', payment_status = ?
        WHERE id = ?
    """, (new_pay, res["id"]))

    text = f"Booking #{res['id']} (slot {res['slot']}) cancelled by {by}."
    if refunded:
        text += f" Refunded Rs.{res['price']}."
    notify(db, "info", text)
    return refunded


def user_is_valid(user_id):
    with get_db() as db:
        row = db.execute("SELECT blocked FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is not None and not row["blocked"]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Please log in first."}), 401
        if not user_is_valid(session["user_id"]):
            session.clear()
            return jsonify({"error": "Your account is blocked or no longer exists."}), 403
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Please log in first."}), 401
        if not user_is_valid(session["user_id"]):
            session.clear()
            return jsonify({"error": "Your account is blocked or no longer exists."}), 403
        if session.get("role") != "admin":
            return jsonify({"error": "Admin access required."}), 403
        return view(*args, **kwargs)
    return wrapped


def fmt_ts(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""


def parse_range():
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def parse(value, default):
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except (TypeError, ValueError):
            return default

    start = parse(request.args.get("from"), midnight - timedelta(days=29))
    end = parse(request.args.get("to"), midnight)
    if end < start:
        start, end = end, start
    t0 = int(start.timestamp())
    t1 = int((end + timedelta(days=1)).timestamp()) - 1
    return t0, t1, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ==========================================
# PAGES
# ==========================================

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/admin")
def admin_page():
    return render_template("admin.html")


@app.route("/receipt/<int:res_id>")
def receipt_page(res_id):
    return render_template("receipt.html")


@app.route("/api/config")
def config():
    with get_db() as db:
        cfg = get_settings(db)
    return jsonify({
        "rate": cfg["rate"],
        "max_hours": cfg["max_hours"],
        "hold_seconds": cfg["hold_seconds"],
        "lot_name": cfg["lot_name"],
    })


# ==========================================
# ACCOUNTS
# ==========================================

@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if not USERNAME_PATTERN.match(username):
        return jsonify({"error": "Username must be 3-20 letters, numbers or underscores."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400
    if username.lower() == ADMIN_USERNAME.lower():
        return jsonify({"error": "This username is reserved."}), 400

    try:
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'user', ?)",
                (username, generate_password_hash(password), int(time.time())),
            )
            user_id = cur.lastrowid
            notify(db, "info", f"New user registered: {username}")
    except sqlite3.IntegrityError:
        return jsonify({"error": "That username is already taken."}), 409

    session.clear()
    session["user_id"] = user_id
    session["username"] = username
    session["role"] = "user"
    return jsonify({"username": username, "role": "user"})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    with get_db() as db:
        user = db.execute(
            "SELECT id, username, password_hash, role, blocked FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    if user is None or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Wrong username or password."}), 401

    if user["blocked"]:
        return jsonify({"error": "Your account has been blocked. Contact the administrator."}), 403

    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]
    return jsonify({"message": "Login successful", "username": user["username"], "role": user["role"]})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out successfully"})


@app.route("/api/me")
def me():
    if "user_id" not in session:
        return jsonify({"logged_in": False})
    if not user_is_valid(session["user_id"]):
        session.clear()
        return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True,
        "user_id": session["user_id"],
        "username": session["username"],
        "role": session["role"],
    })


# ==========================================
# PARKING (USER SIDE)
# ==========================================

@app.route("/api/slots")
def get_slots():
    current = session.get("user_id")
    with get_db() as db:
        release_expired(db)
        rows = db.execute("""
            SELECT s.id, s.status, s.zone, s.user_id, s.vehicle, s.start_time, s.end_time, s.price,
                   l.id AS res_id, l.payment_status AS pay_status, l.hold_expires
            FROM slots s
            LEFT JOIN reservations_log l
              ON l.id = (SELECT MAX(id) FROM reservations_log
                         WHERE slot = s.id AND user_id = s.user_id
                           AND status IN ('pending', 'active'))
            ORDER BY CAST(SUBSTR(s.id, 2) AS INTEGER)
        """).fetchall()

    slots = []
    for r in rows:
        mine = current is not None and r["user_id"] == current
        reserved = r["status"] == "reserved"
        pending = mine and reserved and r["pay_status"] == "pending"
        slots.append({
            "id": r["id"],
            "status": r["status"],
            "zone": r["zone"],
            "mine": mine,
            "vehicle": r["vehicle"] if mine else None,
            "price": r["price"] if mine else None,
            "start_ms": r["start_time"] * 1000 if mine and reserved and r["start_time"] else None,
            "end_ms": r["end_time"] * 1000 if reserved and r["end_time"] else None,
            "res_id": r["res_id"] if mine and reserved else None,
            "pay_status": r["pay_status"] if mine and reserved else None,
            "hold_end_ms": r["hold_expires"] * 1000 if pending and r["hold_expires"] else None,
        })
    return jsonify(slots)


@app.route("/api/reserve", methods=["POST"])
@login_required
def reserve():
    data = request.get_json() or {}
    slot = str(data.get("slot", ""))
    vehicle = str(data.get("vehicle", "")).replace(" ", "").upper()
    hold = bool(data.get("hold"))   # True = pay-now flow, False = old pay-at-gate flow

    try:
        hours = int(data.get("hours", 0))
    except (TypeError, ValueError):
        hours = 0

    uid = session["user_id"]
    uname = session["username"]

    with get_db() as db:
        cfg = get_settings(db)

        if not vehicle:
            return jsonify({"error": "Vehicle number is required."}), 400
        if not PLATE_PATTERN.match(vehicle):
            return jsonify({"error": "Invalid vehicle number. Example: KA01AB1234"}), 400
        if hours < 1 or hours > cfg["max_hours"]:
            return jsonify({"error": f"Choose a duration between 1 and {cfg['max_hours']} hours."}), 400

        now = int(time.time())
        end = now + hours * 3600
        price = hours * cfg["rate"]
        slot_end = now + cfg["hold_seconds"] if hold else end

        release_expired(db)

        existing = db.execute(
            "SELECT id FROM slots WHERE vehicle = ? AND status = 'reserved'", (vehicle,)
        ).fetchone()
        if existing:
            return jsonify({"error": f"This vehicle already has slot {existing['id']}."}), 409

        cur = db.execute("""
            UPDATE slots
            SET status = 'reserved', user_id = ?, vehicle = ?,
                start_time = ?, end_time = ?, price = ?
            WHERE id = ? AND status = 'available'
        """, (uid, vehicle, now, slot_end, price, slot))

        if cur.rowcount == 0:
            return jsonify({"error": "Sorry, this slot is not available."}), 409

        if hold:
            log = db.execute("""
                INSERT INTO reservations_log
                (user_id, username, slot, vehicle, start_time, end_time, price,
                 status, hours, payment_status, hold_expires)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 'pending', ?)
            """, (uid, uname, slot, vehicle, now, end, price, hours, now + cfg["hold_seconds"]))
            res_id = log.lastrowid
        else:
            txn = new_txn()
            log = db.execute("""
                INSERT INTO reservations_log
                (user_id, username, slot, vehicle, start_time, end_time, price,
                 status, hours, payment_status, payment_method, txn_id, paid_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, 'paid', 'cash', ?, ?)
            """, (uid, uname, slot, vehicle, now, end, price, hours, txn, now))
            res_id = log.lastrowid
            record_payment(db, res_id, uid, price, "cash", "pay at gate", "success", txn)

        check_lot_full(db)

    return jsonify({
        "message": "Held - payment required" if hold else "Reserved",
        "slot": slot,
        "vehicle": vehicle,
        "hours": hours,
        "price": price,
        "res_id": res_id,
        "pending": hold,
        "hold_end_ms": slot_end * 1000 if hold else None,
        "end_ms": end * 1000,
    })


@app.route("/api/pay", methods=["POST"])
@login_required
def pay():
    data = request.get_json() or {}
    slot = str(data.get("slot", ""))
    method = str(data.get("method", ""))
    uid = session["user_id"]

    # ---- validate the (simulated) payment details ----
    card_number = ""
    if method == "upi":
        upi = str(data.get("upi_id", "")).strip()
        if not UPI_PATTERN.match(upi):
            return jsonify({"error": "Enter a valid UPI ID, for example name@upi."}), 400
        detail = upi
    elif method == "card":
        card_number = re.sub(r"\D", "", str(data.get("card_number", "")))
        expiry = str(data.get("expiry", "")).strip()
        cvv = str(data.get("cvv", "")).strip()
        if len(card_number) != 16:
            return jsonify({"error": "Card number must be 16 digits."}), 400
        if not valid_expiry(expiry):
            return jsonify({"error": "Enter a valid expiry date as MM/YY."}), 400
        if not re.fullmatch(r"\d{3}", cvv):
            return jsonify({"error": "CVV must be 3 digits."}), 400
        detail = "card ending " + card_number[-4:]   # full number is never stored
    elif method == "qr":
        detail = "UPI QR scan"
    else:
        return jsonify({"error": "Choose a payment method."}), 400

    with get_db() as db:
        release_expired(db)

        res = db.execute("""
            SELECT id, hours, price FROM reservations_log
            WHERE slot = ? AND user_id = ? AND status = 'pending'
            ORDER BY id DESC LIMIT 1
        """, (slot, uid)).fetchone()

        if res is None:
            return jsonify({
                "error": "No pending booking for this slot. The hold may have expired - please book again."
            }), 404

        txn = new_txn()

        # Simulated bank decline (test card)
        if method == "card" and card_number == DECLINED_CARD:
            record_payment(db, res["id"], uid, res["price"], method, detail, "failed", txn)
            notify(db, "warning",
                   f"Payment failed for booking #{res['id']} (slot {slot}): card declined.")
            return jsonify({"error": "Card declined by the bank. Try another method."}), 402

        # Parking time starts now, at the moment of payment
        now = int(time.time())
        end = now + res["hours"] * 3600

        cur = db.execute("""
            UPDATE slots SET start_time = ?, end_time = ?
            WHERE id = ? AND user_id = ? AND status = 'reserved'
        """, (now, end, slot, uid))
        if cur.rowcount == 0:
            return jsonify({"error": "This booking is no longer active."}), 409

        db.execute("""
            UPDATE reservations_log
            SET status = 'active', payment_status = 'paid', payment_method = ?,
                txn_id = ?, paid_at = ?, start_time = ?, end_time = ?, hold_expires = NULL
            WHERE id = ?
        """, (method, txn, now, now, end, res["id"]))

        record_payment(db, res["id"], uid, res["price"], method, detail, "success", txn)
        notify(db, "info",
               f"Payment received: Rs.{res['price']} for slot {slot} ({method.upper()}, {txn}).")

        row = db.execute(
            "SELECT * FROM reservations_log WHERE id = ?", (res["id"],)
        ).fetchone()
        receipt = receipt_dict(db, row)

    return jsonify({"message": "Payment successful", "receipt": receipt})


@app.route("/api/receipt/<int:res_id>")
@login_required
def receipt(res_id):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM reservations_log WHERE id = ?", (res_id,)
        ).fetchone()
        if row is None or (row["user_id"] != session["user_id"] and session.get("role") != "admin"):
            return jsonify({"error": "Receipt not found."}), 404
        if row["payment_status"] not in ("paid", "refunded"):
            return jsonify({"error": "No receipt for an unpaid booking."}), 404
        data = receipt_dict(db, row)
    return jsonify(data)


@app.route("/api/cancel", methods=["POST"])
@login_required
def cancel():
    slot = str((request.get_json() or {}).get("slot", ""))
    uid = session["user_id"]

    with get_db() as db:
        release_expired(db)

        res = db.execute("""
            SELECT * FROM reservations_log
            WHERE slot = ? AND user_id = ? AND status IN ('pending', 'active')
            ORDER BY id DESC LIMIT 1
        """, (slot, uid)).fetchone()

        if res is None:
            # older booking with no history row: just free the slot
            cur = db.execute("""
                UPDATE slots
                SET status = 'available', user_id = NULL, vehicle = NULL,
                    start_time = NULL, end_time = NULL, price = NULL
                WHERE id = ? AND status = 'reserved' AND user_id = ?
            """, (slot, uid))
            if cur.rowcount == 0:
                return jsonify({"error": "No reservation of yours found."}), 404
            return jsonify({"message": "Reservation cancelled", "refunded": False})

        refunded = cancel_reservation_row(db, res, "user " + session["username"])

    return jsonify({
        "message": "Reservation cancelled. Payment refunded." if refunded else "Reservation cancelled",
        "refunded": refunded,
    })


# ==========================================
# SENSOR INPUT (real hardware or the simulator)
# ==========================================

@app.route("/api/sensor", methods=["POST"])
def sensor_update():
    if request.headers.get("X-API-Key") != SENSOR_KEY:
        with get_db() as db:
            notify(db, "warning", "Sensor update rejected: invalid API key.", 600)
        return jsonify({"error": "Invalid API key."}), 401

    data = request.get_json() or {}
    slot = str(data.get("slot", ""))
    status = str(data.get("status", ""))

    if status not in ("available", "occupied"):
        return jsonify({"error": "Invalid status."}), 400

    with get_db() as db:
        release_expired(db)
        cur = db.execute(
            "UPDATE slots SET status = ? WHERE id = ? AND status != 'reserved'",
            (status, slot),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Slot not found or reserved."}), 404
        check_lot_full(db)

    return jsonify({"message": f"{slot} set to {status}"})


# ==========================================
# ADMIN: DASHBOARD
# ==========================================

@app.route("/api/admin/dashboard")
@admin_required
def admin_dashboard():
    with get_db() as db:
        release_expired(db)

        def count(status):
            return db.execute(
                "SELECT COUNT(*) FROM slots WHERE status = ?", (status,)
            ).fetchone()[0]

        total = db.execute("SELECT COUNT(*) FROM slots").fetchone()[0]
        users = db.execute("SELECT COUNT(*) FROM users WHERE role = 'user'").fetchone()[0]
        revenue = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'success'"
        ).fetchone()[0]
        refunded_amount = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'refunded'"
        ).fetchone()[0]
        total_bookings = db.execute("SELECT COUNT(*) FROM reservations_log").fetchone()[0]
        cancelled = db.execute(
            "SELECT COUNT(*) FROM reservations_log WHERE status = 'cancelled'"
        ).fetchone()[0]
        pending = db.execute(
            "SELECT COUNT(*) FROM reservations_log WHERE status = 'pending'"
        ).fetchone()[0]

        midnight = int(datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())
        today_revenue = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'success' AND created_at >= ?",
            (midnight,),
        ).fetchone()[0]
        unread = db.execute(
            "SELECT COUNT(*) FROM notifications WHERE is_read = 0"
        ).fetchone()[0]
        blocked_users = db.execute(
            "SELECT COUNT(*) FROM users WHERE blocked = 1"
        ).fetchone()[0]

    return jsonify({
        "total_slots": total,
        "available_slots": count("available"),
        "reserved_slots": count("reserved"),
        "occupied_slots": count("occupied"),
        "total_users": users,
        "blocked_users": blocked_users,
        "total_revenue": revenue,
        "today_revenue": today_revenue,
        "refunded_amount": refunded_amount,
        "total_bookings": total_bookings,
        "cancelled_bookings": cancelled,
        "pending_payments": pending,
        "unread_notifications": unread,
    })


# ==========================================
# ADMIN: MANAGE PARKING SLOTS
# ==========================================

@app.route("/api/admin/slots")
@admin_required
def admin_slots():
    with get_db() as db:
        release_expired(db)
        rows = db.execute("""
            SELECT s.id, s.status, s.zone, s.vehicle, u.username
            FROM slots s LEFT JOIN users u ON s.user_id = u.id
            ORDER BY CAST(SUBSTR(s.id, 2) AS INTEGER)
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/slots", methods=["POST"])
@admin_required
def admin_add_slots():
    data = request.get_json() or {}
    zone = str(data.get("zone") or "General").strip()

    try:
        count = int(data.get("count", 1))
    except (TypeError, ValueError):
        count = 0

    if zone not in ZONES:
        return jsonify({"error": "Invalid zone."}), 400
    if count < 1 or count > 20:
        return jsonify({"error": "You can add 1 to 20 slots at a time."}), 400

    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM slots").fetchone()[0]
        if total + count > MAX_SLOTS:
            return jsonify({"error": f"A lot can have at most {MAX_SLOTS} slots."}), 400

        last = db.execute(
            "SELECT MAX(CAST(SUBSTR(id, 2) AS INTEGER)) FROM slots"
        ).fetchone()[0] or 0

        added = []
        for i in range(1, count + 1):
            sid = f"P{last + i}"
            db.execute(
                "INSERT INTO slots (id, status, zone) VALUES (?, 'available', ?)", (sid, zone)
            )
            added.append(sid)

        notify(db, "info", f"Admin added {count} slot(s): {', '.join(added)} ({zone}).")

    return jsonify({"message": f"Added {', '.join(added)}.", "added": added})


@app.route("/api/admin/slots/<slot_id>", methods=["POST"])
@admin_required
def admin_update_slot(slot_id):
    data = request.get_json() or {}
    status = data.get("status")
    zone = data.get("zone")

    if status is None and zone is None:
        return jsonify({"error": "Nothing to update."}), 400
    if status is not None and status not in ("available", "occupied"):
        return jsonify({"error": "Invalid slot status."}), 400
    if zone is not None:
        zone = str(zone).strip()
        if zone not in ZONES:
            return jsonify({"error": "Invalid zone."}), 400

    with get_db() as db:
        row = db.execute("SELECT status FROM slots WHERE id = ?", (slot_id,)).fetchone()
        if row is None:
            return jsonify({"error": "Slot not found."}), 404

        if status is not None and row["status"] == "reserved":
            return jsonify({"error": "Slot is currently reserved."}), 409

        if zone is not None:
            db.execute("UPDATE slots SET zone = ? WHERE id = ?", (zone, slot_id))

        if status is not None:
            db.execute("""
                UPDATE slots
                SET status = ?, user_id = NULL, vehicle = NULL,
                    start_time = NULL, end_time = NULL, price = NULL
                WHERE id = ?
            """, (status, slot_id))
            check_lot_full(db)

    return jsonify({"message": f"{slot_id} updated successfully."})


@app.route("/api/admin/slots/<slot_id>", methods=["DELETE"])
@admin_required
def admin_delete_slot(slot_id):
    with get_db() as db:
        row = db.execute("SELECT status FROM slots WHERE id = ?", (slot_id,)).fetchone()
        if row is None:
            return jsonify({"error": "Slot not found."}), 404
        if row["status"] == "reserved":
            return jsonify({"error": "Slot is currently reserved. Cancel the booking first."}), 409

        db.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        notify(db, "info", f"Admin deleted slot {slot_id}.")

    return jsonify({"message": f"{slot_id} deleted."})


# ==========================================
# ADMIN: MANAGE USERS
# ==========================================

@app.route("/api/admin/users")
@admin_required
def admin_users():
    with get_db() as db:
        rows = db.execute("""
            SELECT u.id, u.username, u.role, u.blocked, u.created_at,
                   (SELECT COUNT(*) FROM reservations_log l WHERE l.user_id = u.id) AS bookings,
                   (SELECT COALESCE(SUM(p.amount), 0) FROM payments p
                    WHERE p.user_id = u.id AND p.status = 'success') AS spent
            FROM users u ORDER BY u.id DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/users/<int:user_id>/block", methods=["POST"])
@admin_required
def admin_block_user(user_id):
    blocked = 1 if (request.get_json() or {}).get("blocked") else 0

    with get_db() as db:
        user = db.execute(
            "SELECT username, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user is None:
            return jsonify({"error": "User not found."}), 404
        if user["role"] == "admin":
            return jsonify({"error": "Admin accounts cannot be blocked."}), 400

        db.execute("UPDATE users SET blocked = ? WHERE id = ?", (blocked, user_id))
        notify(db, "warning" if blocked else "info",
               f"User {user['username']} was {'blocked' if blocked else 'unblocked'} by admin.")

    return jsonify({"message": "User blocked." if blocked else "User unblocked."})


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def admin_delete_user(user_id):
    with get_db() as db:
        user = db.execute(
            "SELECT username, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user is None:
            return jsonify({"error": "User not found."}), 404
        if user["role"] == "admin":
            return jsonify({"error": "Admin accounts cannot be deleted."}), 400

        active = db.execute("""
            SELECT COUNT(*) FROM reservations_log
            WHERE user_id = ? AND status IN ('pending', 'active')
        """, (user_id,)).fetchone()[0]
        if active:
            return jsonify({"error": "User has an active booking. Cancel it first."}), 409

        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        notify(db, "warning", f"User {user['username']} was deleted by admin.")

    return jsonify({"message": "User deleted."})


# ==========================================
# ADMIN: BOOKING MANAGEMENT
# ==========================================

@app.route("/api/admin/reservations")
@admin_required
def admin_reservations():
    with get_db() as db:
        release_expired(db)
        rows = db.execute("""
            SELECT slots.id AS slot, slots.status, slots.vehicle,
                   slots.start_time, slots.end_time, slots.price, users.username,
                   (SELECT l.payment_status FROM reservations_log l
                    WHERE l.slot = slots.id AND l.user_id = slots.user_id
                      AND l.status IN ('pending', 'active')
                    ORDER BY l.id DESC LIMIT 1) AS payment_status
            FROM slots LEFT JOIN users ON slots.user_id = users.id
            WHERE slots.status = 'reserved'
            ORDER BY slots.start_time DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/history")
@admin_required
def admin_history():
    with get_db() as db:
        release_expired(db)
        rows = db.execute("""
            SELECT id, username, slot, vehicle, start_time, end_time, price, status,
                   payment_status, payment_method, txn_id
            FROM reservations_log ORDER BY id DESC LIMIT 100
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/bookings")
@admin_required
def admin_bookings():
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    where, args = [], []
    if status in ("pending", "active", "completed", "cancelled", "expired"):
        where.append("status = ?")
        args.append(status)
    if q:
        like = f"%{q}%"
        where.append("(username LIKE ? OR vehicle LIKE ? OR slot LIKE ? OR txn_id LIKE ?)")
        args += [like, like, like, like]

    sql = """
        SELECT id, username, slot, vehicle, hours, price, start_time, end_time, status,
               payment_status, payment_method, txn_id
        FROM reservations_log
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT 200"

    with get_db() as db:
        release_expired(db)
        rows = db.execute(sql, args).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/bookings/<int:res_id>/cancel", methods=["POST"])
@admin_required
def admin_cancel_booking(res_id):
    with get_db() as db:
        release_expired(db)
        res = db.execute("""
            SELECT * FROM reservations_log
            WHERE id = ? AND status IN ('pending', 'active')
        """, (res_id,)).fetchone()
        if res is None:
            return jsonify({"error": "Only pending or active bookings can be cancelled."}), 404

        refunded = cancel_reservation_row(db, res, "admin")

    return jsonify({
        "message": "Booking cancelled and payment refunded." if refunded else "Booking cancelled.",
        "refunded": refunded,
    })


@app.route("/api/admin/bookings/<int:res_id>/complete", methods=["POST"])
@admin_required
def admin_complete_booking(res_id):
    with get_db() as db:
        release_expired(db)
        res = db.execute("""
            SELECT * FROM reservations_log WHERE id = ? AND status = 'active'
        """, (res_id,)).fetchone()
        if res is None:
            return jsonify({"error": "Only active bookings can be ended early."}), 404

        free_slot(db, res["slot"], res["user_id"])
        db.execute("UPDATE reservations_log SET status = 'completed' WHERE id = ?", (res_id,))
        notify(db, "info", f"Booking #{res_id} (slot {res['slot']}) ended early by admin.")

    return jsonify({"message": "Booking ended and slot released."})


@app.route("/api/admin/payments")
@admin_required
def admin_payments():
    with get_db() as db:
        release_expired(db)
        rows = db.execute("""
            SELECT p.id, p.reservation_id, u.username, p.amount, p.method,
                   p.detail, p.status, p.txn_id, p.created_at
            FROM payments p LEFT JOIN users u ON p.user_id = u.id
            ORDER BY p.id DESC LIMIT 100
        """).fetchall()
    return jsonify([dict(r) for r in rows])


# ==========================================
# ADMIN: REVENUE ANALYTICS
# ==========================================

@app.route("/api/admin/analytics")
@admin_required
def admin_analytics():
    with get_db() as db:
        release_expired(db)

        rev_rows = db.execute("""
            SELECT date(created_at, 'unixepoch', 'localtime') AS day,
                   SUM(amount) AS total
            FROM payments WHERE status = 'success'
            GROUP BY day
        """).fetchall()

        hour_rows = db.execute("""
            SELECT strftime('%H', start_time, 'unixepoch', 'localtime') AS hr,
                   COUNT(*) AS n
            FROM reservations_log
            WHERE start_time IS NOT NULL AND status != 'expired'
            GROUP BY hr
        """).fetchall()

        method_rows = db.execute("""
            SELECT method, COUNT(*) AS n FROM payments
            WHERE status = 'success' GROUP BY method
        """).fetchall()

    revenue_by_day = {r["day"]: r["total"] for r in rev_rows}
    days, revenue = [], []
    for i in range(6, -1, -1):
        d = datetime.fromtimestamp(time.time() - i * 86400)
        days.append(d.strftime("%d %b"))
        revenue.append(revenue_by_day.get(d.strftime("%Y-%m-%d"), 0))

    hours = [0] * 24
    for r in hour_rows:
        hours[int(r["hr"])] = r["n"]

    return jsonify({
        "days": days,
        "revenue": revenue,
        "hours": hours,
        "methods": {(r["method"] or "other").upper(): r["n"] for r in method_rows},
    })


@app.route("/api/admin/revenue")
@admin_required
def admin_revenue():
    period = request.args.get("period", "daily")
    if period not in ("daily", "weekly", "monthly"):
        period = "daily"

    with get_db() as db:
        rows = db.execute(
            "SELECT created_at, amount FROM payments WHERE status = 'success'"
        ).fetchall()

    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())

    daily, weekly, monthly = {}, {}, {}
    today_total = week_total = month_total = all_time = 0

    for r in rows:
        d = datetime.fromtimestamp(r["created_at"]).date()
        amt = r["amount"]
        all_time += amt
        daily[d] = daily.get(d, 0) + amt
        wk = d - timedelta(days=d.weekday())
        weekly[wk] = weekly.get(wk, 0) + amt
        monthly[(d.year, d.month)] = monthly.get((d.year, d.month), 0) + amt
        if d == today:
            today_total += amt
        if wk == week_start:
            week_total += amt
        if (d.year, d.month) == (today.year, today.month):
            month_total += amt

    labels, values = [], []
    if period == "daily":
        for i in range(29, -1, -1):
            d = today - timedelta(days=i)
            labels.append(d.strftime("%d %b"))
            values.append(daily.get(d, 0))
    elif period == "weekly":
        for i in range(11, -1, -1):
            ws = week_start - timedelta(weeks=i)
            labels.append("Wk " + ws.strftime("%d %b"))
            values.append(weekly.get(ws, 0))
    else:
        y, m = today.year, today.month
        months = []
        for _ in range(12):
            months.append((y, m))
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        months.reverse()
        for (y, m) in months:
            labels.append(datetime(y, m, 1).strftime("%b %Y"))
            values.append(monthly.get((y, m), 0))

    return jsonify({
        "period": period,
        "labels": labels,
        "values": values,
        "period_total": sum(values),
        "today": today_total,
        "week": week_total,
        "month": month_total,
        "all_time": all_time,
    })


# ==========================================
# ADMIN: REPORTS
# ==========================================

@app.route("/api/admin/reports/summary")
@admin_required
def report_summary():
    t0, t1, d0, d1 = parse_range()

    with get_db() as db:
        release_expired(db)

        def one(sql, args=()):
            return db.execute(sql, args).fetchone()[0]

        in_range = (t0, t1)
        total = one("SELECT COUNT(*) FROM reservations_log WHERE start_time BETWEEN ? AND ?", in_range)

        by_status = {}
        for s in ("active", "completed", "cancelled", "expired", "pending"):
            by_status[s] = one(
                "SELECT COUNT(*) FROM reservations_log WHERE start_time BETWEEN ? AND ? AND status = ?",
                (t0, t1, s),
            )

        revenue = one(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'success' AND created_at BETWEEN ? AND ?",
            in_range,
        )
        refunded = one(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'refunded' AND created_at BETWEEN ? AND ?",
            in_range,
        )
        failed = one(
            "SELECT COUNT(*) FROM payments WHERE status = 'failed' AND created_at BETWEEN ? AND ?",
            in_range,
        )
        avg_hours = one("""
            SELECT COALESCE(AVG(hours), 0) FROM reservations_log
            WHERE start_time BETWEEN ? AND ? AND status IN ('active', 'completed')
        """, in_range)
        new_users = one(
            "SELECT COUNT(*) FROM users WHERE role = 'user' AND created_at BETWEEN ? AND ?",
            in_range,
        )

        busiest = db.execute("""
            SELECT strftime('%H', start_time, 'unixepoch', 'localtime') AS hr, COUNT(*) AS n
            FROM reservations_log
            WHERE start_time BETWEEN ? AND ? AND status != 'expired'
            GROUP BY hr ORDER BY n DESC LIMIT 1
        """, in_range).fetchone()

        top_users = db.execute("""
            SELECT username, COUNT(*) AS bookings, COALESCE(SUM(price), 0) AS spent
            FROM reservations_log
            WHERE start_time BETWEEN ? AND ? AND payment_status = 'paid'
            GROUP BY username ORDER BY spent DESC LIMIT 5
        """, in_range).fetchall()

        slot_usage = db.execute("""
            SELECT slot, COUNT(*) AS bookings
            FROM reservations_log
            WHERE start_time BETWEEN ? AND ? AND status IN ('active', 'completed')
            GROUP BY slot ORDER BY bookings DESC LIMIT 5
        """, in_range).fetchall()

    return jsonify({
        "from": d0,
        "to": d1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_bookings": total,
        "by_status": by_status,
        "revenue": revenue,
        "refunded": refunded,
        "failed_payments": failed,
        "avg_hours": round(avg_hours, 1),
        "new_users": new_users,
        "busiest_hour": (busiest["hr"] + ":00") if busiest else None,
        "top_users": [dict(r) for r in top_users],
        "top_slots": [dict(r) for r in slot_usage],
    })


@app.route("/api/admin/export/<kind>")
@admin_required
def admin_export(kind):
    t0, t1, d0, d1 = parse_range()

    with get_db() as db:
        release_expired(db)

        if kind == "bookings":
            rows = db.execute("""
                SELECT id, username, slot, vehicle, hours, price, start_time, end_time,
                       status, payment_status, payment_method, txn_id
                FROM reservations_log
                WHERE start_time BETWEEN ? AND ? ORDER BY id
            """, (t0, t1)).fetchall()
            return csv_response(
                f"bookings_{d0}_to_{d1}.csv",
                ["ID", "User", "Slot", "Vehicle", "Hours", "Amount", "Start", "End",
                 "Booking status", "Payment status", "Method", "Transaction ID"],
                [[r["id"], r["username"], r["slot"], r["vehicle"], r["hours"], r["price"],
                  fmt_ts(r["start_time"]), fmt_ts(r["end_time"]), r["status"],
                  r["payment_status"], r["payment_method"] or "", r["txn_id"] or ""]
                 for r in rows],
            )

        if kind == "payments":
            rows = db.execute("""
                SELECT p.id, p.reservation_id, u.username, p.amount, p.method, p.detail,
                       p.status, p.txn_id, p.created_at
                FROM payments p LEFT JOIN users u ON p.user_id = u.id
                WHERE p.created_at BETWEEN ? AND ? ORDER BY p.id
            """, (t0, t1)).fetchall()
            return csv_response(
                f"payments_{d0}_to_{d1}.csv",
                ["ID", "Booking", "User", "Amount", "Method", "Detail", "Status",
                 "Transaction ID", "Time"],
                [[r["id"], r["reservation_id"], r["username"] or "", r["amount"],
                  r["method"] or "", r["detail"] or "", r["status"], r["txn_id"] or "",
                  fmt_ts(r["created_at"])] for r in rows],
            )

        if kind == "users":
            rows = db.execute("""
                SELECT u.id, u.username, u.role, u.blocked, u.created_at,
                       (SELECT COUNT(*) FROM reservations_log l WHERE l.user_id = u.id) AS bookings,
                       (SELECT COALESCE(SUM(p.amount), 0) FROM payments p
                        WHERE p.user_id = u.id AND p.status = 'success') AS spent
                FROM users u ORDER BY u.id
            """).fetchall()
            return csv_response(
                "users.csv",
                ["ID", "Username", "Role", "Blocked", "Registered", "Bookings", "Total spent"],
                [[r["id"], r["username"], r["role"], "yes" if r["blocked"] else "no",
                  fmt_ts(r["created_at"]), r["bookings"], r["spent"]] for r in rows],
            )

    return jsonify({"error": "Unknown report type."}), 404


# ==========================================
# ADMIN: NOTIFICATIONS
# ==========================================

@app.route("/api/admin/notifications")
@admin_required
def admin_notifications():
    with get_db() as db:
        release_expired(db)
        rows = db.execute("""
            SELECT id, created_at, level, message, is_read
            FROM notifications ORDER BY id DESC LIMIT 100
        """).fetchall()
        unread = db.execute(
            "SELECT COUNT(*) FROM notifications WHERE is_read = 0"
        ).fetchone()[0]
    return jsonify({"unread": unread, "items": [dict(r) for r in rows]})


@app.route("/api/admin/notifications/read", methods=["POST"])
@admin_required
def admin_notifications_read():
    with get_db() as db:
        db.execute("UPDATE notifications SET is_read = 1")
    return jsonify({"message": "All notifications marked as read."})


@app.route("/api/admin/notifications/clear", methods=["POST"])
@admin_required
def admin_notifications_clear():
    with get_db() as db:
        db.execute("DELETE FROM notifications")
    return jsonify({"message": "Notifications cleared."})


# ==========================================
# ADMIN: SETTINGS
# ==========================================

@app.route("/api/admin/settings")
@admin_required
def admin_get_settings():
    with get_db() as db:
        cfg = get_settings(db)
    return jsonify({
        "rate": cfg["rate"],
        "max_hours": cfg["max_hours"],
        "hold_minutes": cfg["hold_seconds"] // 60,
        "lot_name": cfg["lot_name"],
    })


@app.route("/api/admin/settings", methods=["POST"])
@admin_required
def admin_save_settings():
    data = request.get_json() or {}

    try:
        rate = int(data.get("rate"))
        max_hours = int(data.get("max_hours"))
        hold_minutes = int(data.get("hold_minutes"))
    except (TypeError, ValueError):
        return jsonify({"error": "Rate, maximum hours and hold time must be whole numbers."}), 400

    lot_name = str(data.get("lot_name", "")).strip()

    if not 1 <= rate <= 1000:
        return jsonify({"error": "Hourly rate must be between 1 and 1000."}), 400
    if not 1 <= max_hours <= 72:
        return jsonify({"error": "Maximum booking must be between 1 and 72 hours."}), 400
    if not 1 <= hold_minutes <= 60:
        return jsonify({"error": "Payment hold must be between 1 and 60 minutes."}), 400
    if not 1 <= len(lot_name) <= 40:
        return jsonify({"error": "Lot name must be 1 to 40 characters."}), 400

    with get_db() as db:
        for key, value in (
            ("rate_per_hour", rate),
            ("max_hours", max_hours),
            ("hold_minutes", hold_minutes),
            ("lot_name", lot_name),
        ):
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))
            )
        notify(db, "info",
               f"Settings updated: Rs.{rate}/hour, max {max_hours} hours, hold {hold_minutes} min.")

    return jsonify({"message": "Settings saved. New bookings use these values."})


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )