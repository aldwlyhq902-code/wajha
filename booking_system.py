"""
نظام الحجز الكامل (Booking System) — خادم Flask + SQLite، بلا اعتماديات جديدة.
==============================================================================
ميزات:
  • تقويم مواعيد فعلي: توليد فترات حجز من ساعات عمل المنشأة وطولها وسعتها.
  • منع تعارض ذرّي: لا يُحجز موعد ممتلئ (قفل + إعادة تحقق داخل المعاملة).
  • صفحة حجز عامة لكل منشأة + لوحة إدارة محميّة برقم PIN.
  • أنواع حجز متكيّفة: مطعم=طاولة، عيادة/صالون=موعد، فندق=غرفة.
  • إشعارات: سجل + بريد SMTP اختياري (env) + رابط واتساب + ملف تقويم .ics للعميل.
  • استيراد المنشآت مباشرةً من بيانات السحب (output/*.json).

الاستخدام:
    python booking_system.py import           # استورد من output/*.json
    python booking_system.py import output/x.json --country 966
    python booking_system.py list             # اعرض المنشآت + روابطها + PIN
    python booking_system.py run              # شغّل الخادم على http://localhost:5001

بريد الإشعارات (اختياري): عيّن المتغيرات قبل run:
    BOOKING_SMTP_HOST, BOOKING_SMTP_PORT, BOOKING_SMTP_USER,
    BOOKING_SMTP_PASS, BOOKING_NOTIFY_EMAIL
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import threading
import time
from datetime import datetime, date as date_cls, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from flask import (
    Flask, request, session, redirect, url_for,
    jsonify, render_template_string, abort, Response,
)

# إعادة استخدام أدوات الهاتف والتحميل من محرّك العملاء
from leads import normalize_phone, whatsappable, load_records

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("booking")

# --------------------------------------------------------------------------- #
# الإعداد                                                                      #
# --------------------------------------------------------------------------- #
# مسار البيانات قابل للضبط: على Render وجّهه لقرص دائم (BOOKING_DATA_DIR=/var/data)
DATA_DIR = Path(os.environ.get("BOOKING_DATA_DIR", "booking_data"))
DB_PATH = DATA_DIR / "booking.db"
SECRET_PATH = DATA_DIR / ".secret"
MAX_AHEAD_DAYS = 60

TYPE_LABELS = {
    "table":       {"title": "احجز طاولة", "count_on": True,  "count_label": "عدد الأشخاص", "service_on": False, "service_label": ""},
    "appointment": {"title": "احجز موعدك", "count_on": False, "count_label": "",           "service_on": True,  "service_label": "الخدمة المطلوبة"},
    "room":        {"title": "احجز غرفة",  "count_on": True,  "count_label": "عدد الضيوف",  "service_on": False, "service_label": ""},
    "generic":     {"title": "احجز الآن",  "count_on": True,  "count_label": "عدد الأشخاص", "service_on": False, "service_label": ""},
}

_FOOD = ("مطعم", "مقهى", "كافيه", "كوفي", "قهوة", "مأكولات", "برجر", "بيتزا", "cafe", "restaurant", "coffee")
_CLINIC = ("عياد", "أسنان", "طبيب", "مستشفى", "طب", "مختبر", "clinic", "dentist", "medical")
_SALON = ("صالون", "حلاق", "تجميل", "سبا", "مساج", "salon", "spa", "barber", "beauty")
_HOTEL = ("فندق", "نزل", "شقق", "منتجع", "استراحة", "hotel", "resort", "suites")


def detect_type(category: str) -> str:
    c = (category or "").lower()
    if any(k in c for k in _FOOD):
        return "table"
    if any(k in c for k in _CLINIC) or any(k in c for k in _SALON):
        return "appointment"
    if any(k in c for k in _HOTEL):
        return "room"
    return "generic"


# --------------------------------------------------------------------------- #
# طبقة قاعدة البيانات                                                          #
# --------------------------------------------------------------------------- #
_book_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# طبقة الاتصال: SQLite محلي افتراضياً، أو Turso (libSQL) إن ضُبطت متغيّرات Turso #
# تُحفظ البيانات دائماً في Turso (مناسب للاستضافة بلا قرص دائم مثل Render المجاني) #
# --------------------------------------------------------------------------- #
def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


class _LibsqlCursor:
    """واجهة شبيهة بمؤشّر sqlite3 فوق نتيجة libsql_client."""
    def __init__(self, rs=None):
        self._rows = list(rs.rows) if rs is not None else []
        self.rowcount = getattr(rs, "rows_affected", -1) if rs is not None else -1
        self.lastrowid = getattr(rs, "last_insert_rowid", None) if rs is not None else None
        self._i = 0

    def fetchone(self):
        if self._i < len(self._rows):
            r = self._rows[self._i]
            self._i += 1
            return r
        return None

    def fetchall(self):
        rest = self._rows[self._i:]
        self._i = len(self._rows)
        return list(rest)

    def __iter__(self):
        return iter(self._rows[self._i:])


class _LibsqlConn:
    """واجهة شبيهة باتصال sqlite3 فوق عميل libsql_client (Turso).

    صفوف libsql تدعم الوصول بالاسم والفهرس، فلا حاجة لـ row_factory.
    كل عبارة تُنفَّذ تلقائياً (autocommit)؛ منع التعارض يبقى صحيحاً عبر _book_lock
    لأن الخدمة تعمل بعملية واحدة (gunicorn --workers 1 + threads).
    """
    def __init__(self, client):
        self._client = client

    def execute(self, sql, params=()):
        args = list(params) if params else None
        rs = self._client.execute(sql, args) if args else self._client.execute(sql)
        return _LibsqlCursor(rs)

    def executescript(self, script):
        stmts = _split_sql(script)
        try:
            self._client.batch(stmts)
        except Exception:
            for s in stmts:
                self._client.execute(s)
        return _LibsqlCursor(None)

    def commit(self):
        pass  # autocommit

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, value):
        pass  # غير مطلوب: صفوف libsql تدعم الوصول بالاسم أصلاً


def get_db():
    turso = os.environ.get("TURSO_DATABASE_URL")
    if turso:
        import libsql_client
        token = os.environ.get("TURSO_AUTH_TOKEN")
        client = (libsql_client.create_client_sync(url=turso, auth_token=token)
                  if token else libsql_client.create_client_sync(url=turso))
        return _LibsqlConn(client)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT UNIQUE NOT NULL,
  feature_id TEXT,
  name TEXT NOT NULL,
  category TEXT, phone TEXT, whatsapp TEXT,
  address TEXT, lat REAL, lng REAL, image_url TEXT,
  rating REAL, reviews_count INTEGER,
  booking_type TEXT DEFAULT 'generic',
  open_time TEXT DEFAULT '09:00',
  close_time TEXT DEFAULT '23:00',
  slot_minutes INTEGER DEFAULT 30,
  capacity INTEGER DEFAULT 5,
  pin_hash TEXT, pin_salt TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id INTEGER NOT NULL,
  ref TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  customer_phone TEXT NOT NULL,
  date TEXT NOT NULL, time TEXT NOT NULL,
  party INTEGER, service TEXT, notes TEXT,
  status TEXT DEFAULT 'pending',
  created_at TEXT,
  FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_bk ON bookings(business_id, date, time, status);
"""


# أعمدة الاشتراك — تُضاف عبر الترحيل للقواعد الموجودة دون فقدان بيانات
SUB_COLUMNS = {
    "plan": "TEXT DEFAULT 'تجريبي'",
    "sub_status": "TEXT DEFAULT 'trial'",   # trial / active / suspended / cancelled
    "monthly_fee": "REAL DEFAULT 0",
    "sub_started": "TEXT",
    "sub_renews": "TEXT",
}


def _migrate(conn) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()}
    for col, decl in SUB_COLUMNS.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE businesses ADD COLUMN {col} {decl}")


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


def _hash_pin(pin: str, salt: str) -> str:
    return hashlib.sha256((salt + pin).encode("utf-8")).hexdigest()


def _slugify(name: str, fid: str) -> str:
    s = re.sub(r"[^\w؀-ۿ]+", "-", name or "").strip("-")[:32]
    suffix = (fid.split(":")[-1][-6:] if fid else secrets.token_hex(3))
    return f"{s or 'place'}-{suffix}".strip("-").lower()


def _feature_id(url: str) -> str:
    m = re.search(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", url or "")
    return m.group(0) if m else ""


def get_business(slug: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM businesses WHERE slug=?", (slug,)).fetchone()
    conn.close()
    return row


def list_businesses():
    conn = get_db()
    rows = conn.execute("SELECT * FROM businesses ORDER BY name").fetchall()
    conn.close()
    return rows


# ---- توليد الفترات + التوفّر ---------------------------------------------- #
def _hm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def generate_slots(open_t: str, close_t: str, step: int) -> list[str]:
    try:
        start, end = _hm_to_min(open_t), _hm_to_min(close_t)
    except Exception:
        start, end = 9 * 60, 23 * 60
    step = max(5, int(step or 30))
    if end <= start:
        end = start + 60
    slots, t = [], start
    while t < end:
        slots.append("%02d:%02d" % (t // 60, t % 60))
        t += step
    return slots


def slot_usage(conn, business_id: int, day: str) -> dict:
    rows = conn.execute(
        "SELECT time, COUNT(*) c FROM bookings "
        "WHERE business_id=? AND date=? AND status!='cancelled' GROUP BY time",
        (business_id, day),
    ).fetchall()
    return {r["time"]: r["c"] for r in rows}


def available_slots(conn, biz, day: str) -> list[str]:
    all_slots = generate_slots(biz["open_time"], biz["close_time"], biz["slot_minutes"])
    used = slot_usage(conn, biz["id"], day)
    cap = biz["capacity"] or 1
    out = [s for s in all_slots if used.get(s, 0) < cap]
    # لا تُظهر فترات اليوم التي مضت
    if day == datetime.now().strftime("%Y-%m-%d"):
        now = datetime.now().strftime("%H:%M")
        out = [s for s in out if s > now]
    return out


def _valid_date(day: str) -> str | None:
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except Exception:
        return "تاريخ غير صالح"
    today = date_cls.today()
    if d < today:
        return "لا يمكن الحجز في تاريخ ماضٍ"
    if d > today + timedelta(days=MAX_AHEAD_DAYS):
        return f"الحجز متاح حتى {MAX_AHEAD_DAYS} يوماً مقدّماً فقط"
    return None


def create_booking(biz, payload) -> tuple[dict | None, str | None]:
    """ينشئ حجزاً مع منع التعارض ذرّياً (قفل + إعادة تحقق داخل المعاملة)."""
    name = (payload.get("name") or "").strip()
    phone = (payload.get("phone") or "").strip()
    day = (payload.get("date") or "").strip()
    tm = (payload.get("time") or "").strip()
    if not name or not phone:
        return None, "الاسم ورقم الجوال مطلوبان"
    err = _valid_date(day)
    if err:
        return None, err
    if not re.match(r"^\d{1,2}:\d{2}$", tm):
        return None, "وقت غير صالح"

    party = payload.get("party")
    try:
        party = int(party) if party not in (None, "", "أكثر من 6") else (7 if party == "أكثر من 6" else None)
    except (TypeError, ValueError):
        party = None
    service = (payload.get("service") or "").strip()[:120]
    notes = (payload.get("notes") or "").strip()[:300]

    with _book_lock:
        conn = get_db()
        try:
            if tm not in available_slots(conn, biz, day):
                return None, "هذا الموعد لم يعد متاحاً، اختر وقتاً آخر"
            ref = secrets.token_hex(3).upper()
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO bookings(business_id,ref,customer_name,customer_phone,"
                "date,time,party,service,notes,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'pending', ?)",
                (biz["id"], ref, name, phone[:25], day, tm, party, service, notes, now),
            )
            conn.commit()
        finally:
            conn.close()
    booking = {"ref": ref, "name": name, "phone": phone, "date": day, "time": tm,
               "party": party, "service": service, "notes": notes}
    return booking, None


def list_bookings(business_id: int, upcoming_only: bool = False):
    conn = get_db()
    q = "SELECT * FROM bookings WHERE business_id=?"
    args = [business_id]
    if upcoming_only:
        q += " AND date>=?"
        args.append(datetime.now().strftime("%Y-%m-%d"))
    q += " ORDER BY date, time"
    rows = conn.execute(q, args).fetchall()
    conn.close()
    return rows


def set_booking_status(business_id: int, booking_id: int, status: str) -> bool:
    if status not in ("confirmed", "cancelled", "pending"):
        return False
    conn = get_db()
    cur = conn.execute("UPDATE bookings SET status=? WHERE id=? AND business_id=?",
                       (status, booking_id, business_id))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def update_settings(business_id: int, open_t, close_t, slot, cap) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE businesses SET open_time=?, close_time=?, slot_minutes=?, capacity=? WHERE id=?",
        (open_t, close_t, int(slot), int(cap), business_id),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# الإشعارات                                                                    #
# --------------------------------------------------------------------------- #
def notify_new_booking(biz, booking) -> None:
    logger.info("🔔 حجز جديد [%s] %s | %s %s | %s (%s)",
                biz["name"], booking["ref"], booking["date"], booking["time"],
                booking["name"], booking["phone"])

    # إشعار واتساب فوري للمنشأة عبر WaSenderAPI (إن توفّر المفتاح ورقم واتساب)
    if os.environ.get("WASENDER_API_KEY") and biz["whatsapp"]:
        try:
            from wasender import WaSenderClient
            txt = (f"🔔 حجز جديد ({booking['ref']}) لدى {biz['name']}\n"
                   f"العميل: {booking['name']} - {booking['phone']}\n"
                   f"الموعد: {booking['date']} {booking['time']}\n"
                   f"التفاصيل: {booking.get('party') or booking.get('service') or '-'}")
            res = WaSenderClient(min_interval=0).send_text("+" + biz["whatsapp"], txt)
            if res.get("ok"):
                logger.info("📲 أُرسل إشعار واتساب للمنشأة")
            else:
                logger.warning("تعذّر إشعار واتساب: %s", res.get("error"))
        except Exception as e:
            logger.warning("تعذّر إشعار واتساب: %s", e)

    host = os.environ.get("BOOKING_SMTP_HOST")
    to = os.environ.get("BOOKING_NOTIFY_EMAIL")
    if not (host and to):
        return
    try:
        body = (f"حجز جديد لدى {biz['name']}\n"
                f"المرجع: {booking['ref']}\nالعميل: {booking['name']} - {booking['phone']}\n"
                f"التاريخ: {booking['date']} {booking['time']}\n"
                f"التفاصيل: {booking.get('party') or booking.get('service') or '-'}\n"
                f"ملاحظات: {booking.get('notes') or '-'}")
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"حجز جديد: {biz['name']} ({booking['ref']})"
        msg["From"] = os.environ.get("BOOKING_SMTP_USER", "booking@localhost")
        msg["To"] = to
        port = int(os.environ.get("BOOKING_SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.starttls()
            user = os.environ.get("BOOKING_SMTP_USER")
            pw = os.environ.get("BOOKING_SMTP_PASS")
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        logger.info("✉️ تم إرسال إشعار البريد إلى %s", to)
    except Exception as e:
        logger.warning("تعذّر إرسال إشعار البريد: %s", e)


def build_ics(biz, b) -> str:
    start = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
    end = start + timedelta(minutes=int(biz["slot_minutes"] or 60))
    fmt = lambda d: d.strftime("%Y%m%dT%H%M%S")
    uid = f"{b['ref']}@booking"
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//booking//ar\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTART:{fmt(start)}\r\nDTEND:{fmt(end)}\r\n"
        f"SUMMARY:حجز - {biz['name']}\r\n"
        f"DESCRIPTION:مرجع الحجز {b['ref']} باسم {b['customer_name'] if 'customer_name' in b.keys() else b.get('name','')}\r\n"
        f"LOCATION:{biz['address'] or ''}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )


# --------------------------------------------------------------------------- #
# تطبيق Flask                                                                  #
# --------------------------------------------------------------------------- #
def _load_secret() -> str:
    env = os.environ.get("BOOKING_SECRET")
    if env:
        return env
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        return SECRET_PATH.read_text().strip()
    s = secrets.token_hex(32)
    SECRET_PATH.write_text(s)
    return s


app = Flask(__name__)
app.secret_key = _load_secret()


def _biz_or_404(slug):
    biz = get_business(slug)
    if not biz:
        abort(404)
    return biz


def is_admin(slug: str) -> bool:
    return bool(session.get("admin_" + slug))


def labels_for(biz) -> dict:
    return TYPE_LABELS.get(biz["booking_type"], TYPE_LABELS["generic"])


# ---- عام: الفهرس + صفحة الحجز --------------------------------------------- #
@app.route("/")
def home():
    return render_template_string(INDEX_HTML, businesses=list_businesses())


@app.route("/api/health")
def health():
    """فحص صحّة + نوع قاعدة البيانات (دون كشف أي أسرار)."""
    backend = "turso" if os.environ.get("TURSO_DATABASE_URL") else "sqlite"
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return jsonify({"ok": True, "backend": backend,
                        "businesses": len(list_businesses())})
    except Exception as e:
        # نُرجع 200 مع تفاصيل الخطأ ليكون التشخيص ممكناً من المتصفح
        return jsonify({"ok": False, "backend": backend, "error": str(e)[:300]})


@app.route("/b/<slug>")
def booking_page(slug):
    biz = _biz_or_404(slug)
    today = datetime.now().strftime("%Y-%m-%d")
    maxd = (date_cls.today() + timedelta(days=MAX_AHEAD_DAYS)).strftime("%Y-%m-%d")
    return render_template_string(PUBLIC_HTML, b=biz, lbl=labels_for(biz),
                                  today=today, maxd=maxd)


@app.route("/api/b/<slug>/slots")
def api_slots(slug):
    biz = _biz_or_404(slug)
    day = request.args.get("date", "")
    err = _valid_date(day)
    if err:
        return jsonify({"slots": [], "error": err})
    conn = get_db()
    try:
        slots = available_slots(conn, biz, day)
    finally:
        conn.close()
    return jsonify({"date": day, "slots": slots})


@app.route("/api/b/<slug>/book", methods=["POST"])
def api_book(slug):
    biz = _biz_or_404(slug)
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "بيانات غير صالحة"}), 400
    booking, err = create_booking(biz, data)
    if err:
        return jsonify({"error": err}), 400
    notify_new_booking(biz, booking)
    # رابط واتساب لتأكيد العميل مع المنشأة (إن توفّر رقم واتساب)
    wa = ""
    if biz["whatsapp"]:
        msg = (f"تأكيد حجز ({booking['ref']}) لدى {biz['name']}\n"
               f"الاسم: {booking['name']}\nالتاريخ: {booking['date']} {booking['time']}")
        from urllib.parse import quote
        wa = f"https://wa.me/{biz['whatsapp']}?text={quote(msg)}"
    return jsonify({
        "ok": True, "ref": booking["ref"],
        "ics_url": url_for("api_ics", slug=slug, ref=booking["ref"]),
        "whatsapp_url": wa,
        "msg": f"تم استلام طلبك بنجاح. رقم الحجز: {booking['ref']} — بانتظار تأكيد المنشأة.",
    })


@app.route("/api/b/<slug>/ics/<ref>")
def api_ics(slug, ref):
    biz = _biz_or_404(slug)
    conn = get_db()
    b = conn.execute("SELECT * FROM bookings WHERE business_id=? AND ref=?",
                     (biz["id"], ref)).fetchone()
    conn.close()
    if not b:
        abort(404)
    ics = build_ics(biz, b)
    return Response(ics, mimetype="text/calendar",
                    headers={"Content-Disposition": f"attachment; filename={ref}.ics"})


# ---- إدارة المنشأة -------------------------------------------------------- #
@app.route("/admin/<slug>", methods=["GET"])
def admin_dash(slug):
    biz = _biz_or_404(slug)
    if not is_admin(slug):
        return render_template_string(LOGIN_HTML, b=biz, error=None)
    bookings = list_bookings(biz["id"], upcoming_only=False)
    pending = sum(1 for x in bookings if x["status"] == "pending")
    return render_template_string(ADMIN_HTML, b=biz, bookings=bookings,
                                  pending=pending, lbl=labels_for(biz))


@app.route("/admin/<slug>/login", methods=["POST"])
def admin_login(slug):
    biz = _biz_or_404(slug)
    pin = (request.form.get("pin") or "").strip()
    if biz["pin_hash"] and _hash_pin(pin, biz["pin_salt"]) == biz["pin_hash"]:
        session["admin_" + slug] = True
        return redirect(url_for("admin_dash", slug=slug))
    return render_template_string(LOGIN_HTML, b=biz, error="رقم PIN غير صحيح")


@app.route("/admin/<slug>/logout")
def admin_logout(slug):
    session.pop("admin_" + slug, None)
    return redirect(url_for("admin_dash", slug=slug))


@app.route("/api/admin/<slug>/booking/<int:bid>", methods=["POST"])
def admin_set_status(slug, bid):
    biz = _biz_or_404(slug)
    if not is_admin(slug):
        return jsonify({"error": "غير مصرّح"}), 403
    data = request.get_json(force=True, silent=True) or {}
    ok = set_booking_status(biz["id"], bid, data.get("status", ""))
    return jsonify({"ok": ok}) if ok else (jsonify({"error": "تعذّر التحديث"}), 400)


@app.route("/api/admin/<slug>/settings", methods=["POST"])
def admin_settings(slug):
    biz = _biz_or_404(slug)
    if not is_admin(slug):
        return jsonify({"error": "غير مصرّح"}), 403
    f = request.get_json(force=True, silent=True) or {}
    try:
        update_settings(biz["id"], f["open_time"], f["close_time"],
                        int(f["slot_minutes"]), int(f["capacity"]))
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "قيم غير صالحة"}), 400
    return jsonify({"ok": True})


@app.route("/api/admin/<slug>/bookings")
def admin_bookings_json(slug):
    biz = _biz_or_404(slug)
    if not is_admin(slug):
        return jsonify({"error": "غير مصرّح"}), 403
    rows = list_bookings(biz["id"])
    return jsonify({"pending": sum(1 for r in rows if r["status"] == "pending"),
                    "total": len(rows)})


# --------------------------------------------------------------------------- #
# لوحة المالك (SaaS) — كل العملاء والاشتراكات والإيراد                         #
# --------------------------------------------------------------------------- #
# تتبّع محاولات الدخول الفاشلة لكلمة مرور المالك (دفاع ضد التخمين)
_owner_login_fails: dict[str, list] = {}


def _owner_pw_record() -> str:
    """سجل كلمة مرور المالك على القرص بصيغة salt:hash (لا نص صريح).

    يولّد كلمة قوية (~144 بت) ويطبعها مرة واحدة عند الإنشاء، ويُرقّي أي سجل
    قديم ضعيف/نصّي. لا يُخزَّن النص الصريح إطلاقاً.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = DATA_DIR / ".owner_pw"
    if p.exists():
        rec = p.read_text(encoding="utf-8").strip()
        if rec.count(":") == 1 and len(rec.split(":", 1)[0]) >= 16:
            return rec                      # سجل سليم (salt:hash)
        # غير ذلك: سجل قديم (نص صريح/ضعيف) → يُعاد توليده بأمان
    pw = secrets.token_urlsafe(24)
    salt = secrets.token_hex(16)
    p.write_text(f"{salt}:{_hash_pin(pw, salt)}", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    print("=" * 60)
    print(f" 👑 كلمة مرور المالك (تُعرض مرة واحدة فقط — احفظها): {pw}")
    print("=" * 60)
    return p.read_text(encoding="utf-8").strip()


def _verify_owner_pw(pw: str) -> bool:
    """مقارنة آمنة (تعمل مع المحارف غير اللاتينية أيضاً)."""
    env = os.environ.get("BOOKING_OWNER_PASSWORD")
    if env:
        return secrets.compare_digest(pw.encode("utf-8"), env.encode("utf-8"))
    rec = _owner_pw_record()
    if ":" not in rec:
        return False
    salt, h = rec.split(":", 1)
    return secrets.compare_digest(_hash_pin(pw, salt), h)


def is_owner() -> bool:
    return bool(session.get("owner"))


def owner_stats() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    bizs = conn.execute("SELECT * FROM businesses ORDER BY name").fetchall()
    bk = conn.execute(
        "SELECT business_id, COUNT(*) total, "
        "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending, "
        "SUM(CASE WHEN date>=? AND status!='cancelled' THEN 1 ELSE 0 END) upcoming "
        "FROM bookings GROUP BY business_id", (today,)
    ).fetchall()
    conn.close()
    bkmap = {r["business_id"]: r for r in bk}
    rows = []
    mrr = active = trial = total_bk = pending_all = 0
    for b in bizs:
        s = bkmap.get(b["id"])
        t = (s["total"] if s else 0) or 0
        p = (s["pending"] if s else 0) or 0
        u = (s["upcoming"] if s else 0) or 0
        total_bk += t
        pending_all += p
        st = b["sub_status"] or "trial"
        if st == "active":
            active += 1
            mrr += (b["monthly_fee"] or 0)
        elif st == "trial":
            trial += 1
        rows.append({"id": b["id"], "name": b["name"], "slug": b["slug"],
                     "plan": b["plan"] or "", "status": st,
                     "fee": b["monthly_fee"] or 0, "renews": b["sub_renews"] or "",
                     "total": t, "pending": p, "upcoming": u})
    return {"rows": rows, "clients": len(bizs), "active": active, "trial": trial,
            "mrr": round(mrr) if math.isfinite(mrr) else 0,
            "total_bk": total_bk, "pending": pending_all}


@app.route("/owner")
def owner_dash():
    if not is_owner():
        return render_template_string(OWNER_LOGIN_HTML, error=None)
    return render_template_string(OWNER_HTML, **owner_stats())


@app.route("/owner/login", methods=["POST"])
def owner_login():
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "?")
          .split(",")[0].strip())
    now = time.time()
    cnt, until = _owner_login_fails.get(ip, [0, 0.0])
    if now < until:
        return render_template_string(OWNER_LOGIN_HTML, error="محاولات كثيرة، حاول لاحقاً"), 429
    pw = (request.form.get("pw") or "").strip()
    if pw and _verify_owner_pw(pw):
        _owner_login_fails.pop(ip, None)
        session["owner"] = True
        return redirect(url_for("owner_dash"))
    cnt += 1
    # اسمح بعدة محاولات سريعة (أخطاء مطبعية)، ثم تباطؤ تصاعدي حتى 5 دقائق
    if cnt >= 5:
        _owner_login_fails[ip] = [cnt, now + min(300, 2 ** (cnt - 4))]
    else:
        _owner_login_fails[ip] = [cnt, 0.0]
    return render_template_string(OWNER_LOGIN_HTML, error="كلمة المرور غير صحيحة")


@app.route("/owner/logout")
def owner_logout():
    session.pop("owner", None)
    return redirect(url_for("owner_dash"))


@app.route("/api/owner/sub/<int:bid>", methods=["POST"])
def owner_update_sub(bid):
    if not is_owner():
        return jsonify({"error": "غير مصرّح"}), 403
    f = request.get_json(force=True, silent=True) or {}
    status = f.get("status")
    if status not in ("trial", "active", "suspended", "cancelled"):
        return jsonify({"error": "حالة اشتراك غير صالحة"}), 400
    try:
        fee = float(f.get("fee", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "قيمة الرسوم غير صالحة"}), 400
    if not math.isfinite(fee) or fee < 0 or fee > 1_000_000:
        return jsonify({"error": "قيمة الرسوم غير صالحة"}), 400
    renews = (f.get("renews") or "").strip()
    if renews:
        try:
            datetime.strptime(renews, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "تاريخ غير صالح"}), 400
    plan = (f.get("plan") or "").strip()[:40]
    conn = get_db()
    cur = conn.execute(
        "UPDATE businesses SET plan=?, sub_status=?, monthly_fee=?, sub_renews=? WHERE id=?",
        (plan, status, fee, renews or None, bid))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return jsonify({"ok": ok}) if ok else (jsonify({"error": "منشأة غير موجودة"}), 404)


@app.route("/api/owner/import", methods=["POST"])
def owner_import():
    if not is_owner():
        return jsonify({"error": "غير مصرّح"}), 403
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "ارفع ملف JSON الناتج من السحب (output/*.json)"}), 400
    try:
        data = json.loads(f.read().decode("utf-8"))
    except Exception:
        return jsonify({"error": "ملف JSON غير صالح"}), 400
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return jsonify({"error": "صيغة غير متوقعة (المتوقع قائمة منشآت)"}), 400
    results = import_records(data, "966")
    added = [r for r in results if r["added"]]
    return jsonify({
        "added": len(added),
        "skipped": len(results) - len(added),
        "businesses": [{"name": r["name"], "slug": r["slug"], "pin": r.get("pin")} for r in added],
    })


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def import_records(records, country: str = "966") -> list[dict]:
    """يُدخل المنشآت للقاعدة (يتخطّى المكرّر). يُعيد [{name, slug, pin, added}].

    تُستخدم من سطر الأوامر ومن لوحة المالك (رفع JSON) على حدٍ سواء.
    """
    init_db()
    conn = get_db()
    out: list[dict] = []
    try:
        for r in records:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            fid = _feature_id(r.get("place_url", ""))
            slug = _slugify(r.get("name", ""), fid)
            exists = conn.execute(
                "SELECT 1 FROM businesses WHERE slug=? OR (feature_id!='' AND feature_id=?)",
                (slug, fid)).fetchone()
            if exists:
                out.append({"name": r.get("name", ""), "slug": slug, "added": False})
                continue
            intl = normalize_phone(r.get("phone"), country)
            wa = intl if (intl and whatsappable(intl, r.get("phone"), country)) else ""
            salt = secrets.token_hex(8)
            pin = "%04d" % secrets.randbelow(10000)
            today = date_cls.today().isoformat()
            renews = (date_cls.today() + timedelta(days=14)).isoformat()  # تجربة 14 يوماً
            conn.execute(
                "INSERT INTO businesses(slug,feature_id,name,category,phone,whatsapp,address,"
                "lat,lng,image_url,rating,reviews_count,booking_type,pin_hash,pin_salt,created_at,"
                "plan,sub_status,monthly_fee,sub_started,sub_renews) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (slug, fid, r.get("name", ""), r.get("category", ""), r.get("phone", ""), wa,
                 r.get("address", ""), r.get("latitude"), r.get("longitude"), r.get("image_url", ""),
                 r.get("rating"), r.get("reviews_count"), detect_type(r.get("category", "")),
                 _hash_pin(pin, salt), salt, datetime.now().isoformat(timespec="seconds"),
                 "تجريبي", "trial", 0, today, renews),
            )
            out.append({"name": r.get("name", ""), "slug": slug, "pin": pin, "added": True})
        conn.commit()
    finally:
        conn.close()
    return out


def cmd_import(args) -> None:
    records = load_records(args.input)
    if not records:
        print("❌ لا توجد سجلات في", args.input)
        return
    results = import_records(records, args.country)
    for r in results:
        if r["added"]:
            print(f"  ✓ {r['name'][:34]:<34} | /b/{r['slug']}  | PIN: {r['pin']}")
    added = sum(1 for r in results if r["added"])
    print(f"\nتمت إضافة {added} منشأة (تجاهل {len(results) - added} مكرّرة).")
    print("شغّل الخادم:  python booking_system.py run   ← ثم افتح http://localhost:5001")


def cmd_list(args) -> None:
    init_db()
    rows = list_businesses()
    if not rows:
        print("لا توجد منشآت. استورد أولاً:  python booking_system.py import")
        return
    print("=" * 70)
    for b in rows:
        wa = "📲" if b["whatsapp"] else "—"
        print(f" {b['name'][:32]:<32} | {b['booking_type']:<11} | {wa} | /b/{b['slug']}")
    print("=" * 70)
    print(f"{len(rows)} منشأة. لوحة الإدارة: /admin/<slug>  (رقم PIN ظهر عند الاستيراد)")


def cmd_run(args) -> None:
    init_db()
    n = len(list_businesses())
    _owner_pw_record()  # تأكّد من وجود كلمة مرور المالك (وتُطبع مرة عند الإنشاء)
    print("=" * 60)
    print(" 🗓️  نظام الحجز — الخادم يعمل")
    print(f" المنشآت: {n}  |  افتح: http://localhost:{args.port}")
    print(f" 👑 لوحة المالك: http://localhost:{args.port}/owner")
    if not os.environ.get("BOOKING_OWNER_PASSWORD"):
        print("    (كلمة مرور المالك في booking_data/.owner_pw)")
    print("=" * 60)
    app.run(host="127.0.0.1", port=args.port, debug=False)


def build_parser():
    p = argparse.ArgumentParser(description="نظام الحجز الكامل")
    sub = p.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="استيراد المنشآت من بيانات السحب")
    imp.add_argument("input", nargs="*", default=["output/*.json"],
                     help="ملفات JSON (الافتراضي output/*.json)")
    imp.add_argument("--country", "-c", default="966")
    imp.set_defaults(func=cmd_import)
    lst = sub.add_parser("list", help="عرض المنشآت")
    lst.set_defaults(func=cmd_list)
    run = sub.add_parser("run", help="تشغيل الخادم")
    run.add_argument("--port", "-p", type=int, default=5001)
    run.set_defaults(func=cmd_run)
    return p


# --------------------------------------------------------------------------- #
# القوالب (HTML)                                                               #
# --------------------------------------------------------------------------- #
_BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0;font-family:"Segoe UI",Tahoma,"Cairo",sans-serif}
body{background:#eef1f4;color:#1f2933;line-height:1.6}
.wrap{max-width:880px;margin:0 auto;padding:24px}
.card{background:#fff;border:1px solid #e3e8ee;border-radius:14px;padding:20px;margin-bottom:16px}
h1{font-size:24px}h2{font-size:18px;margin-bottom:12px}.muted{color:#7b8794}
label{display:block;font-size:13px;font-weight:600;color:#5b6b7b;margin:10px 0 5px}
input,select,textarea{width:100%;padding:11px;border:1px solid #d4dbe2;border-radius:10px;font-size:15px;font-family:inherit}
input:focus,select:focus,textarea:focus{outline:0;border-color:#1a73e8;box-shadow:0 0 0 3px rgba(26,115,232,.13)}
.btn{display:inline-block;text-align:center;text-decoration:none;padding:11px 18px;border-radius:10px;font-weight:700;font-size:15px;border:0;cursor:pointer}
.btn.p{background:#1a73e8;color:#fff}.btn.g{background:#25d366;color:#fff}.btn.d{background:#fce8e6;color:#c5221f}
.btn.ok{background:#e6f4ea;color:#137333}
table{width:100%;border-collapse:collapse}th,td{padding:10px;text-align:right;border-bottom:1px solid #eef1f4;font-size:14px}
th{color:#7b8794;font-size:13px}
.pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600}
.pill.pending{background:#fef7e0;color:#b06000}.pill.confirmed{background:#e6f4ea;color:#137333}.pill.cancelled{background:#fce8e6;color:#c5221f}
.slot{display:inline-block;margin:4px;padding:8px 14px;border:1px solid #d4dbe2;border-radius:10px;cursor:pointer;background:#fff}
.slot.sel{background:#1a73e8;color:#fff;border-color:#1a73e8}.slot:hover{border-color:#1a73e8}
.f2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
a{color:#1a73e8}
"""

INDEX_HTML = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>نظام الحجز</title>
<style>""" + _BASE_CSS + """</style></head><body><div class="wrap">
<h1>🗓️ نظام الحجز</h1><p class="muted" style="margin-bottom:16px">المنشآت المتاحة للحجز:</p>
{% for b in businesses %}
<div class="card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
  <div><strong style="font-size:16px">{{ b['name'] }}</strong>
   <div class="muted" style="font-size:13px">{{ b['category'] }} · {{ b['booking_type'] }}</div></div>
  <div><a class="btn p" href="/b/{{ b['slug'] }}">احجز</a>
   <a class="btn" style="background:#eef1f4;color:#3b4a5a" href="/admin/{{ b['slug'] }}">إدارة</a></div>
</div>
{% else %}<div class="card">لا توجد منشآت بعد. استورد:  <code>python booking_system.py import</code></div>{% endfor %}
</div></body></html>"""

PUBLIC_HTML = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{{ b['name'] }} — حجز</title>
<style>""" + _BASE_CSS + """
.hero{background:linear-gradient(135deg,#1a73e8,#34a853);color:#fff;border-radius:14px;padding:24px;margin-bottom:16px}
.hero h1{font-size:26px}.hero .r{font-size:14px;opacity:.95}
</style></head><body><div class="wrap">
<div class="hero"><div style="opacity:.9;font-size:14px">{{ b['category'] }}</div>
  <h1>{{ b['name'] }}</h1>
  <div class="r">{% if b['rating'] %}★ {{ b['rating'] }} {% if b['reviews_count'] %}({{ b['reviews_count'] }} مراجعة){% endif %}{% endif %}
   {% if b['address'] %}· {{ b['address'] }}{% endif %}</div></div>

<div class="card"><h2>{{ lbl['title'] }}</h2>
  <label>اختر التاريخ</label>
  <input type="date" id="date" min="{{ today }}" max="{{ maxd }}" value="{{ today }}">
  <label>الأوقات المتاحة</label>
  <div id="slots" class="muted">اختر تاريخاً لعرض الأوقات…</div>
  <input type="hidden" id="time">
  <div id="form" style="display:none;margin-top:8px">
    <div class="f2">
      <div><label>الاسم</label><input id="name" placeholder="اسمك الكريم"></div>
      <div><label>رقم الجوال</label><input id="phone" inputmode="tel" placeholder="05xxxxxxxx"></div>
    </div>
    {% if lbl['count_on'] %}<label>{{ lbl['count_label'] }}</label>
    <select id="party"><option>1</option><option>2</option><option>3</option><option>4</option>
     <option>5</option><option>6</option><option>أكثر من 6</option></select>{% endif %}
    {% if lbl['service_on'] %}<label>{{ lbl['service_label'] }}</label>
    <input id="service" placeholder="{{ lbl['service_label'] }}">{% endif %}
    <label>ملاحظات (اختياري)</label><textarea id="notes" rows="2"></textarea>
    <button class="btn g" style="width:100%;margin-top:14px" onclick="book()">تأكيد الحجز</button>
  </div>
  <div id="done" style="display:none"></div>
</div></div>
<script>
const SLUG="{{ b['slug'] }}";
const dateEl=document.getElementById('date');
dateEl.addEventListener('change',loadSlots);
async function loadSlots(){
  document.getElementById('time').value=''; document.getElementById('form').style.display='none';
  const s=document.getElementById('slots'); s.textContent='جارٍ التحميل…';
  try{
    const r=await fetch(`/api/b/${SLUG}/slots?date=${dateEl.value}`); const d=await r.json();
    if(d.error){s.textContent=d.error;return;}
    if(!d.slots.length){s.textContent='لا توجد أوقات متاحة في هذا اليوم.';return;}
    s.innerHTML=d.slots.map(t=>`<span class="slot" onclick="pick(this,'${t}')">${t}</span>`).join('');
  }catch(e){s.textContent='تعذّر تحميل الأوقات.';}
}
function pick(el,t){
  document.querySelectorAll('.slot').forEach(x=>x.classList.remove('sel'));
  el.classList.add('sel'); document.getElementById('time').value=t;
  document.getElementById('form').style.display='block';
}
async function book(){
  const g=id=>{const e=document.getElementById(id);return e?e.value.trim():'';};
  const body={name:g('name'),phone:g('phone'),date:dateEl.value,time:g('time'),
    party:g('party'),service:g('service'),notes:g('notes')};
  if(!body.time){alert('اختر وقتاً');return;}
  if(!body.name||!body.phone){alert('أدخل الاسم ورقم الجوال');return;}
  const r=await fetch(`/api/b/${SLUG}/book`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(!r.ok){alert(d.error||'تعذّر الحجز');loadSlots();return;}
  const wa=d.whatsapp_url?`<a class="btn g" href="${d.whatsapp_url}" target="_blank">تأكيد عبر واتساب</a>`:'';
  document.querySelector('.card').innerHTML=
    `<h2>✅ تم استلام طلبك</h2><p>${d.msg}</p>
     <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap">
       <a class="btn p" href="${d.ics_url}">أضِف للتقويم</a>${wa}
       <a class="btn" style="background:#eef1f4;color:#3b4a5a" href="/b/${SLUG}">حجز آخر</a></div>`;
}
loadSlots();
</script></body></html>"""

LOGIN_HTML = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>دخول الإدارة</title>
<style>""" + _BASE_CSS + """</style></head><body><div class="wrap" style="max-width:420px">
<div class="card"><h2>🔒 إدارة: {{ b['name'] }}</h2>
{% if error %}<p style="color:#c5221f;margin-bottom:8px">{{ error }}</p>{% endif %}
<form method="post" action="/admin/{{ b['slug'] }}/login">
  <label>رقم PIN</label><input name="pin" type="password" inputmode="numeric" autofocus>
  <button class="btn p" style="width:100%;margin-top:14px">دخول</button>
</form></div></div></body></html>"""

ADMIN_HTML = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>إدارة {{ b['name'] }}</title>
<style>""" + _BASE_CSS + """</style></head><body><div class="wrap">
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:14px">
  <h1>📋 {{ b['name'] }}</h1>
  <div><a class="btn p" href="/b/{{ b['slug'] }}" target="_blank">صفحة الحجز</a>
   <a class="btn" style="background:#eef1f4;color:#3b4a5a" href="/admin/{{ b['slug'] }}/logout">خروج</a></div>
</div>

<div class="card"><h2>⚙️ الإعدادات <span id="newbadge" class="pill pending" style="display:none">حجوزات جديدة!</span></h2>
  <div class="f2">
    <div><label>من الساعة</label><input id="open_time" type="time" value="{{ b['open_time'] }}"></div>
    <div><label>إلى الساعة</label><input id="close_time" type="time" value="{{ b['close_time'] }}"></div>
    <div><label>مدة الفترة (دقيقة)</label><input id="slot_minutes" type="number" min="5" step="5" value="{{ b['slot_minutes'] }}"></div>
    <div><label>السعة لكل فترة</label><input id="capacity" type="number" min="1" value="{{ b['capacity'] }}"></div>
  </div>
  <button class="btn p" style="margin-top:12px" onclick="saveSettings()">حفظ الإعدادات</button>
  <span id="ssmsg" class="muted"></span>
</div>

<div class="card"><h2>الحجوزات ({{ bookings|length }}) — بانتظار التأكيد: {{ pending }}</h2>
<table><thead><tr><th>المرجع</th><th>التاريخ/الوقت</th><th>العميل</th><th>التفاصيل</th><th>الحالة</th><th>إجراء</th></tr></thead><tbody>
{% for x in bookings %}
<tr id="row{{ x['id'] }}">
  <td>{{ x['ref'] }}</td>
  <td>{{ x['date'] }}<br><span class="muted">{{ x['time'] }}</span></td>
  <td>{{ x['customer_name'] }}<br><a href="tel:{{ x['customer_phone'] }}" class="muted">{{ x['customer_phone'] }}</a></td>
  <td>{% if x['party'] %}{{ lbl['count_label'] }}: {{ x['party'] }}{% endif %}
      {% if x['service'] %}{{ x['service'] }}{% endif %}
      {% if x['notes'] %}<div class="muted" style="font-size:12px">{{ x['notes'] }}</div>{% endif %}</td>
  <td><span class="pill {{ x['status'] }}" id="st{{ x['id'] }}">
      {{ {'pending':'بانتظار','confirmed':'مؤكّد','cancelled':'ملغى'}[x['status']] }}</span></td>
  <td>
    <button class="btn ok" style="padding:6px 10px;font-size:13px" onclick="setStatus({{ x['id'] }},'confirmed')">تأكيد</button>
    <button class="btn d" style="padding:6px 10px;font-size:13px" onclick="setStatus({{ x['id'] }},'cancelled')">إلغاء</button>
  </td>
</tr>
{% else %}<tr><td colspan="6" class="muted" style="text-align:center;padding:24px">لا توجد حجوزات بعد.</td></tr>{% endfor %}
</tbody></table></div></div>
<script>
const SLUG="{{ b['slug'] }}";
let lastPending={{ pending }};
async function setStatus(id,status){
  const r=await fetch(`/api/admin/${SLUG}/booking/${id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  const d=await r.json();
  if(d.ok){const lbls={confirmed:'مؤكّد',cancelled:'ملغى',pending:'بانتظار'};
    const el=document.getElementById('st'+id);el.textContent=lbls[status];el.className='pill '+status;}
  else alert(d.error||'تعذّر التحديث');
}
async function saveSettings(){
  const g=id=>document.getElementById(id).value;
  const r=await fetch(`/api/admin/${SLUG}/settings`,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({open_time:g('open_time'),close_time:g('close_time'),slot_minutes:g('slot_minutes'),capacity:g('capacity')})});
  const d=await r.json();document.getElementById('ssmsg').textContent=d.ok?' ✓ حُفظ':(' '+(d.error||'خطأ'));
}
// تحديث حيّ لإشعار الحجوزات الجديدة
setInterval(async()=>{
  try{const r=await fetch(`/api/admin/${SLUG}/bookings`);const d=await r.json();
    if(d.pending>lastPending){document.getElementById('newbadge').style.display='inline-block';}
    lastPending=d.pending;}catch(e){}
},15000);
</script></body></html>"""


OWNER_LOGIN_HTML = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>لوحة المالك</title>
<style>""" + _BASE_CSS + """</style></head><body><div class="wrap" style="max-width:420px">
<div class="card"><h2>👑 لوحة المالك</h2>
{% if error %}<p style="color:#c5221f;margin-bottom:8px">{{ error }}</p>{% endif %}
<form method="post" action="/owner/login">
  <label>كلمة مرور المالك</label><input name="pw" type="password" autofocus>
  <button class="btn p" style="width:100%;margin-top:14px">دخول</button>
</form>
<p class="muted" style="font-size:12px;margin-top:10px">كلمة المرور تظهر في سجلّ الخادم عند أول تشغيل (أو تُضبط عبر متغيّر البيئة)</p>
</div></div></body></html>"""

OWNER_HTML = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>لوحة المالك</title>
<style>""" + _BASE_CSS + """
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:18px}
.kpi{background:#fff;border:1px solid #e3e8ee;border-radius:14px;padding:14px;text-align:center}
.kpi b{display:block;font-size:24px;color:#1a73e8}.kpi.g b{color:#137333}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#202124;color:#fff;padding:10px 18px;border-radius:10px;display:none}
input,select{padding:6px 8px;font-size:13px}
</style></head><body><div class="wrap">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
  <h1>👑 لوحة المالك</h1><a class="btn" style="background:#eef1f4;color:#3b4a5a" href="/owner/logout">خروج</a></div>
<div class="kpis">
  <div class="kpi"><b>{{ clients }}</b>العملاء</div>
  <div class="kpi g"><b>{{ active }}</b>اشتراك نشط</div>
  <div class="kpi"><b>{{ trial }}</b>تجريبي</div>
  <div class="kpi g"><b>{{ "{:,}".format(mrr) }}</b>الإيراد الشهري (ر)</div>
  <div class="kpi"><b>{{ total_bk }}</b>إجمالي الحجوزات</div>
  <div class="kpi"><b>{{ pending }}</b>بانتظار التأكيد</div>
</div>
<div class="card"><h2>📥 استيراد منشآت</h2>
  <p class="muted" style="font-size:13px;margin-bottom:8px">ارفع ملف JSON الناتج من السحب (output/*.json). يُنشئ لكل منشأة رقم PIN لإدارتها.</p>
  <input type="file" id="impfile" accept=".json,application/json" style="max-width:320px">
  <button class="btn p" style="margin-inline-start:8px" onclick="doImport()">استيراد</button>
  <div id="impres" style="margin-top:10px;font-size:14px"></div>
</div>
<div class="card"><h2>العملاء والاشتراكات</h2>
<table><thead><tr><th>المنشأة</th><th>الباقة</th><th>الحالة</th><th>الرسوم/شهر</th><th>التجديد</th><th>الحجوزات</th><th></th></tr></thead><tbody>
{% for r in rows %}
<tr>
  <td><strong>{{ r.name }}</strong>
    <div class="m"><a href="/admin/{{ r.slug }}" target="_blank">إدارة</a> · <a href="/b/{{ r.slug }}" target="_blank">الصفحة</a></div></td>
  <td><input id="plan{{ r.id }}" value="{{ r.plan }}" style="width:84px"></td>
  <td><select id="st{{ r.id }}">
      <option value="trial" {{ 'selected' if r.status=='trial' else '' }}>تجريبي</option>
      <option value="active" {{ 'selected' if r.status=='active' else '' }}>نشط</option>
      <option value="suspended" {{ 'selected' if r.status=='suspended' else '' }}>موقوف</option>
      <option value="cancelled" {{ 'selected' if r.status=='cancelled' else '' }}>ملغى</option>
    </select></td>
  <td><input id="fee{{ r.id }}" type="number" min="0" step="10" value="{{ r.fee }}" style="width:74px"></td>
  <td><input id="rn{{ r.id }}" type="date" value="{{ r.renews }}"></td>
  <td>{{ r.total }} <span class="m">(قادم {{ r.upcoming }} · انتظار {{ r.pending }})</span></td>
  <td><button class="btn p" style="padding:6px 12px" onclick="saveSub({{ r.id }})">حفظ</button></td>
</tr>
{% else %}<tr><td colspan="7" class="muted" style="text-align:center;padding:24px">لا يوجد عملاء بعد. استورد منشآت أولاً.</td></tr>{% endfor %}
</tbody></table></div></div>
<div class="toast" id="toast"></div>
<script>
async function saveSub(id){
  const g=x=>document.getElementById(x+id).value;
  const r=await fetch('/api/owner/sub/'+id,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({plan:g('plan'),status:g('st'),fee:g('fee'),renews:g('rn')})});
  const d=await r.json();
  const t=document.getElementById('toast');
  t.textContent=d.ok?'تم الحفظ ✓':('خطأ: '+(d.error||''));t.style.display='block';
  setTimeout(()=>t.style.display='none',1500);
}
function _esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function doImport(){
  const fInput=document.getElementById('impfile'); const f=fInput.files[0];
  if(!f){alert('اختر ملف JSON');return;}
  const fd=new FormData(); fd.append('file', f);
  const el=document.getElementById('impres'); el.textContent='جارٍ الاستيراد…';
  try{
    const r=await fetch('/api/owner/import',{method:'POST',body:fd});
    const d=await r.json();
    if(!r.ok){el.textContent='خطأ: '+(d.error||'');return;}
    let html='✓ أُضيف '+d.added+' • تخطّي '+d.skipped+' (مكرّرة)';
    if(d.businesses && d.businesses.length){
      html+='<div class="muted" style="margin:6px 0">احفظ أرقام PIN لتسليمها للمنشآت:</div>';
      html+='<table><tr><th>المنشأة</th><th>PIN</th><th>الرابط</th></tr>';
      d.businesses.forEach(b=>{html+='<tr><td>'+_esc(b.name)+'</td><td><b>'+_esc(b.pin)+'</b></td><td>/b/'+_esc(b.slug)+'</td></tr>';});
      html+='</table><div class="muted" style="margin-top:6px">حدّث الصفحة لرؤيتها في القائمة بالأسفل.</div>';
    }
    el.innerHTML=html;
  }catch(e){el.textContent='تعذّر الاتصال بالخادم';}
}
</script></body></html>"""


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
