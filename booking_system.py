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
import base64
import hashlib
import hmac
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
from html import escape as html_escape
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask, request, session, redirect, url_for,
    jsonify, render_template_string, abort, Response,
)

# إعادة استخدام أدوات الهاتف والتحميل من محرّك العملاء
from leads import normalize_phone, whatsappable, load_records

# وحدات منصّة النمو (بُنيت متوازية): CRM، تدقيق المواقع، التسعير، التقارير، جسر whats_bot
from crm import (ensure_prospects_table, upsert_prospect, mark_sent, record_open,
                 set_status as crm_set_status, list_prospects, stats_by_status, STATUSES,
                 save_audit, set_target, rank_targets, record_click, record_email_open,
                 mark_delivered, mark_read, record_reply)
from audit import audit_site, render_report
from pricing import suggest_plan
from reports import compute_stats, render_dashboard
from whatsbot_bridge import handoff_prospect
from offers import build_message, email_subject
import mailer
from wasender import WaSenderClient, to_e164, load_api_key as _wasender_key

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


class _LibsqlRow:
    """صفّ شبيه بـ sqlite3.Row فوق صفّ libsql: يدعم الوصول بالاسم والفهرس و keys().

    ضروريّ لأن بعض إصدارات libsql_client تُعيد صفوفًا بلا .keys()، فتنكسر الشيفرة
    التي تتوقّع واجهة sqlite3.Row (مثل crm._row_to_dict). نوحّد السلوك عبر كل النقل.
    """
    __slots__ = ("_cols", "_vals", "_map")

    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = vals
        self._map = {c: i for i, c in enumerate(cols)}

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._vals[k]
        return self._vals[self._map[k]]

    def keys(self):
        return list(self._cols)

    def get(self, k, default=None):
        i = self._map.get(k)
        return self._vals[i] if i is not None else default

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __contains__(self, k):
        return k in self._map


class _LibsqlCursor:
    """واجهة شبيهة بمؤشّر sqlite3 فوق نتيجة libsql_client."""
    def __init__(self, rs=None):
        if rs is not None:
            cols = list(getattr(rs, "columns", []) or [])
            self._rows = [_LibsqlRow(cols, list(r)) for r in rs.rows]
        else:
            self._rows = []
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
        # استخدم نقل HTTPS بدل WebSocket (أكثر موثوقية على الاستضافات السحابية)
        turso = turso.strip()
        if turso.startswith("libsql://"):
            turso = "https://" + turso[len("libsql://"):]
        elif turso.startswith("wss://"):
            turso = "https://" + turso[len("wss://"):]
        token = os.environ.get("TURSO_AUTH_TOKEN")
        token = token.strip() if token else token
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
    ensure_prospects_table(conn)   # جدول CRM (العملاء المحتملون/المتابعة)
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
    info = {"backend": backend, "has_token": bool(os.environ.get("TURSO_AUTH_TOKEN")),
            "commit": (os.environ.get("RENDER_GIT_COMMIT") or "local")[:7]}
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        info.update(ok=True, businesses=len(list_businesses()))
    except Exception as e:
        # نُرجع 200 مع تفاصيل الخطأ ليكون التشخيص ممكناً من المتصفح
        info.update(ok=False, error=str(e)[:300])
    return jsonify(info)


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
    # مصادقة: جلسة المالك (من اللوحة) أو مفتاح في الترويسة (للحفظ البرمجي من أداة السحب)
    key = request.headers.get("X-Owner-Key", "")
    if not (is_owner() or (key and _verify_owner_pw(key))):
        return jsonify({"error": "غير مصرّح"}), 403
    # المصدر: جسم JSON مباشر (برمجي) أو ملف مرفوع (من اللوحة)
    if request.is_json:
        data = request.get_json(silent=True)
    else:
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "ارفع ملف JSON أو أرسل جسم JSON"}), 400
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
    # غذِّ مسار الاستهداف/المتابعة (prospects) بنفس عملية الحفظ — حتى تظهر في شاشة الاستهداف
    prospects_added = _feed_prospects(data)
    return jsonify({
        "added": len(added),
        "skipped": len(results) - len(added),
        "prospects": prospects_added,
        "businesses": [{"name": r["name"], "slug": r["slug"], "pin": r.get("pin")} for r in added],
    })


def _feed_prospects(records) -> int:
    """يُدرج/يحدّث سجلّات السحب في جدول prospects (مسار الاستهداف). يُعيد عدد الجدد."""
    conn = get_db()
    try:
        before = conn.execute("SELECT COUNT(*) AS c FROM prospects").fetchone()["c"]
        for r in records:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            intl = normalize_phone(r.get("phone"), "966")
            wa = intl if (intl and whatsappable(intl, r.get("phone"), "966")) else ""
            with _book_lock:
                upsert_prospect(conn, {
                    "feature_id": _feat_id(r.get("place_url", "")),
                    "name": r.get("name", ""), "phone": r.get("phone", ""),
                    "whatsapp": wa, "email": (r.get("email") or "").strip(),
                    "category": r.get("category", ""), "city": r.get("city", ""),
                    "website": r.get("website", ""), "source": "خرائط Google",
                })
        conn.commit()
        after = conn.execute("SELECT COUNT(*) AS c FROM prospects").fetchone()["c"]
    finally:
        conn.close()
    return after - before


# --------------------------------------------------------------------------- #
# CRM (المتابعة) + تدقيق المواقع + التقارير + جسر whats_bot                    #
# --------------------------------------------------------------------------- #
def _get_prospect(conn, pid):
    return conn.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()


def _feat_id(url):
    m = re.search(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", url or "")
    return m.group(0) if m else ""


@app.route("/owner/crm")
def owner_crm():
    if not is_owner():
        return redirect(url_for("owner_dash"))
    sort = request.args.get("sort") or None
    only_targets = request.args.get("targets") in ("1", "true", "yes")
    conn = get_db()
    try:
        prospects = list_prospects(conn, sort=sort, only_targets=only_targets)
        stats = stats_by_status(conn)
        funnel = _funnel_counts(conn)
    finally:
        conn.close()
    return render_template_string(CRM_HTML, prospects=prospects, stats=stats,
                                  statuses=STATUSES, funnel=funnel,
                                  sort=(sort or ""), only_targets=only_targets)


def _funnel_counts(conn) -> dict:
    """عدّادات القمع للعرض البصري: مُستهدَف → أُرسل → فُتح → نُقر → ردّ → عميل."""
    def _c(sql, params=()):
        try:
            return conn.execute(sql, params).fetchone()["c"] or 0
        except Exception:
            return 0
    return {
        "total":   _c("SELECT COUNT(*) AS c FROM prospects"),
        "targets": _c("SELECT COUNT(*) AS c FROM prospects WHERE is_target=1"),
        "sent":    _c("SELECT COUNT(*) AS c FROM prospects WHERE last_contacted_at IS NOT NULL"),
        "opened":  _c("SELECT COUNT(*) AS c FROM prospects WHERE COALESCE(opens,0)>0 OR COALESCE(email_opens,0)>0"),
        "clicked": _c("SELECT COUNT(*) AS c FROM prospects WHERE COALESCE(clicks,0)>0"),
        "replied": _c("SELECT COUNT(*) AS c FROM prospects WHERE status IN ('ردّ','مهتم','عرض','عميل')"),
        "customer": _c("SELECT COUNT(*) AS c FROM prospects WHERE status='عميل'"),
    }


@app.route("/api/owner/prospects/import", methods=["POST"])
def owner_prospects_import():
    if not is_owner():
        return jsonify({"error": "غير مصرّح"}), 403
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "ارفع ملف JSON من السحب"}), 400
    try:
        data = json.loads(f.read().decode("utf-8"))
    except Exception:
        return jsonify({"error": "ملف JSON غير صالح"}), 400
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return jsonify({"error": "صيغة غير متوقعة"}), 400
    conn = get_db()
    try:
        before = conn.execute("SELECT COUNT(*) AS c FROM prospects").fetchone()["c"]
        processed = 0
        for r in data:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            intl = normalize_phone(r.get("phone"), "966")
            wa = intl if (intl and whatsappable(intl, r.get("phone"), "966")) else ""
            with _book_lock:
                upsert_prospect(conn, {
                    "feature_id": _feat_id(r.get("place_url", "")),
                    "name": r.get("name", ""), "phone": r.get("phone", ""),
                    "whatsapp": wa, "category": r.get("category", ""),
                    "website": r.get("website", ""), "source": "خرائط Google",
                })
            processed += 1
        conn.commit()
        after = conn.execute("SELECT COUNT(*) AS c FROM prospects").fetchone()["c"]
    finally:
        conn.close()
    return jsonify({"ok": True, "added": after - before, "updated": processed - (after - before)})


@app.route("/api/owner/prospect/<int:pid>/status", methods=["POST"])
def owner_prospect_status(pid):
    if not is_owner():
        return jsonify({"error": "غير مصرّح"}), 403
    f = request.get_json(force=True, silent=True) or {}
    conn = get_db()
    try:
        ok = crm_set_status(conn, pid, f.get("status", ""), f.get("notes"))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": ok}) if ok else (jsonify({"error": "حالة غير صالحة أو منشأة غير موجودة"}), 400)


@app.route("/api/owner/prospect/<int:pid>/sent", methods=["POST"])
def owner_prospect_sent(pid):
    if not is_owner():
        return jsonify({"error": "غير مصرّح"}), 403
    f = request.get_json(force=True, silent=True) or {}
    conn = get_db()
    try:
        rec = mark_sent(conn, pid, f.get("message", "أُرسلت رسالة"), f.get("report_url"))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": bool(rec)})


@app.route("/owner/prospect/<int:pid>/audit")
def owner_prospect_audit(pid):
    if not is_owner():
        return redirect(url_for("owner_dash"))
    conn = get_db()
    try:
        p = _get_prospect(conn, pid)
        if not p:
            abort(404)
        business = {"name": p["name"], "phone": p["phone"], "website": p["website"]}
        site = (p["website"] or "").strip()
        if not site:
            return Response(
                "<div dir='rtl' style='font-family:Tahoma;padding:40px;text-align:center'>"
                "هذه المنشأة <b>بلا موقع</b> — اعرض خدمة بناء موقع + حجز عبر واتساب 💬</div>",
                mimetype="text/html")
        au = audit_site(site)
        with _book_lock:
            save_audit(conn, pid, au)   # يخزّن الدرجة + نصّ المشاكل + نقاط القوة
            conn.commit()
    finally:
        conn.close()
    return Response(render_report(business, au), mimetype="text/html")


@app.route("/api/owner/prospects/audit", methods=["POST"])
def owner_prospects_bulk_audit():
    """تدقيق جماعي: {ids:[...]} → يدقّق موقع كل عميل، يخزّن الدرجة والملاحظات. يُعيد ملخّصاً."""
    if not is_owner():
        return jsonify({"error": "غير مصرّح"}), 403
    f = request.get_json(force=True, silent=True) or {}
    ids = f.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "لم تُحدَّد عملاء"}), 400
    conn = get_db()
    results = []
    audited = no_site = failed = 0
    try:
        for raw in ids[:60]:   # حدّ أقصى للأمان في الطلب الواحد
            try:
                pid = int(raw)
            except (TypeError, ValueError):
                continue
            p = _get_prospect(conn, pid)
            if not p:
                continue
            site = (p["website"] or "").strip()
            if not site:
                no_site += 1
                with _book_lock:
                    # بلا موقع: درجة 0 وملاحظة واضحة (فرصة بناء موقع جديد)
                    save_audit(conn, pid, {"score": 0,
                                           "issues": ["لا يملك موقعاً إلكترونياً — فرصة بناء موقع + حجز واتساب."],
                                           "strengths": []})
                    conn.commit()
                results.append({"id": pid, "name": p["name"], "score": 0, "no_site": True})
                continue
            try:
                au = audit_site(site)
                with _book_lock:
                    save_audit(conn, pid, au)
                    conn.commit()
                audited += 1
                results.append({"id": pid, "name": p["name"], "score": au.get("score"),
                                "issues": len(au.get("issues") or [])})
            except Exception as e:
                failed += 1
                results.append({"id": pid, "name": p["name"], "error": str(e)[:80]})
    finally:
        conn.close()
    return jsonify({"ok": True, "audited": audited, "no_site": no_site,
                    "failed": failed, "results": results})


@app.route("/api/owner/prospects/target", methods=["POST"])
def owner_prospects_target():
    """تعليم/إلغاء «مُستهدَف» لمجموعة: {ids:[...], on:true|false}."""
    if not is_owner():
        return jsonify({"error": "غير مصرّح"}), 403
    f = request.get_json(force=True, silent=True) or {}
    ids = f.get("ids") or []
    on = bool(f.get("on", True))
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "لم تُحدَّد عملاء"}), 400
    conn = get_db()
    try:
        with _book_lock:
            n = set_target(conn, [int(x) for x in ids if str(x).isdigit()], on)
            rank_targets(conn)   # أعد الترتيب بعد كل تغيير في الاستهداف
            conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "updated": n})


@app.route("/api/owner/prospects/rank", methods=["POST"])
def owner_prospects_rank():
    """يُعيد ترتيب أولوية المُستهدَفين (يُكتب target_rank)."""
    if not is_owner():
        return jsonify({"error": "غير مصرّح"}), 403
    conn = get_db()
    try:
        with _book_lock:
            n = rank_targets(conn)
            conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "ranked": n})


# --------------------------------------------------------------------------- #
# روابط عامّة للعميل: صفحة التقرير + تتبّع النقر (موقّعة برمز لكل عميل)          #
# --------------------------------------------------------------------------- #
def _public_base() -> str:
    """رابط الموقع العام (لبناء روابط التقرير/التتبّع في الرسائل)."""
    base = os.environ.get("BOOKING_PUBLIC_URL", "").strip()
    if base:
        return base.rstrip("/")
    return request.host_url.rstrip("/")


def _prospect_token(pid: int) -> str:
    """رمز توقيع قصير لكل عميل (يمنع تخمين/تعداد روابط التقارير)."""
    mac = hmac.new(app.secret_key.encode("utf-8") if isinstance(app.secret_key, str)
                   else app.secret_key, f"report:{pid}".encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:16]


def _check_token(pid: int, tok: str) -> bool:
    return bool(tok) and secrets.compare_digest(_prospect_token(pid), str(tok))


def report_link_for(pid: int) -> str:
    return f"{_public_base()}/report/{pid}?t={_prospect_token(pid)}"


_NEWSITE_HTML = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>عرض موقع + حجز واتساب — {name}</title>
<style>
body{{margin:0;font-family:Tahoma,Arial,sans-serif;background:#f1f5f9;color:#0f172a;line-height:1.8}}
.wrap{{max-width:600px;margin:0 auto;padding:22px}}
.top{{background:linear-gradient(135deg,#128C7E,#0d6e63);color:#fff;border-radius:18px;padding:26px;text-align:center}}
.top h1{{margin:6px 0;font-size:24px}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:22px;margin-top:16px}}
ul{{padding-inline-start:20px}} li{{margin:8px 0}}
.btn{{display:block;text-align:center;background:#25D366;color:#053d2b;text-decoration:none;font-weight:800;
font-size:18px;padding:15px;border-radius:14px;margin-top:18px}}
</style></head><body><div class="wrap">
<div class="top"><div>واجهة · عرض رقميّ</div><h1>{name}</h1>
<div>موقع إلكترونيّ احترافيّ + حجز عبر واتساب يعمل ٢٤/٧</div></div>
<div class="card"><h2>ماذا نقدّم لكم؟</h2><ul>{services}</ul></div>
<a class="btn" href="{cta}">احجز استشارتك المجانية عبر واتساب</a>
</div>{pixel}</body></html>"""


@app.route("/report/<int:pid>")
def public_report(pid):
    """صفحة عامّة: تقرير تدقيق موقع العميل (أو عرض موقع جديد) — موقّعة برمز، مع تتبّع الفتح."""
    if not _check_token(pid, request.args.get("t", "")):
        abort(404)
    conn = get_db()
    try:
        p = _get_prospect(conn, pid)
        if not p:
            abort(404)
        prospect = _row_to_dict_safe(p)
        with _book_lock:
            record_open(conn, pid)   # فتح الصفحة = إشارة «اطّلع»
            conn.commit()
    finally:
        conn.close()

    pixel = f"{_public_base()}/api/track/open?id={pid}"
    intl = (prospect.get("whatsapp") or "").strip()
    wa_msg = f"السلام عليكم، بخصوص عرض «واجهة» لـ {prospect.get('name','منشأتنا')}"
    wa_target = (f"https://wa.me/{intl}?text={quote(wa_msg)}" if intl
                 else f"https://wa.me/?text={quote(wa_msg)}")
    cta = f"{_public_base()}/go?pid={pid}&t={_prospect_token(pid)}&u={quote(wa_target, safe='')}"

    site = (prospect.get("website") or "").strip()
    if not site:
        from offers import whatsbot_services
        services = "".join(f"<li>{html_escape(s)}</li>" for s in whatsbot_services(prospect.get("category", "")))
        return Response(_NEWSITE_HTML.format(
            name=html_escape(prospect.get("name", "منشأتكم")), services=services,
            cta=html_escape(cta), pixel=f'<img src="{html_escape(pixel)}" width="1" height="1" style="display:none">'),
            mimetype="text/html")

    # لديه موقع: تدقيق حيّ (مع تعويض من المخزّن عند الفشل) ثم تقرير كامل
    au = audit_site(site)
    if not au.get("ok") and prospect.get("audit_issues"):
        au = {"url": site, "ok": True, "score": prospect.get("audit_score") or 0,
              "dims": {}, "issues": (prospect.get("audit_issues") or "").split(" • "),
              "strengths": (prospect.get("audit_strengths") or "").split(" • "),
              "has_booking": False, "has_whatsapp": False, "https": site.startswith("https"),
              "mobile": False, "title": "", "desc": ""}
    business = {"name": prospect.get("name"), "phone": prospect.get("phone"),
                "website": prospect.get("website")}
    return Response(render_report(business, au, cta_url=cta, pixel_url=pixel),
                    mimetype="text/html")


def _row_to_dict_safe(row):
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return dict(row)


@app.route("/go")
def track_click():
    """تتبّع نقر الرابط ثم إعادة التوجيه: ?pid=&t=&u=<urlencoded>."""
    pid = request.args.get("pid")
    tok = request.args.get("t", "")
    url = request.args.get("u", "")
    # السماح فقط بإعادة التوجيه لروابط http/https (منع open-redirect لمخططات أخرى)
    valid = bool(re.match(r"^https?://", url, re.I))
    if valid and pid and pid.isdigit() and _check_token(int(pid), tok):
        conn = get_db()
        try:
            with _book_lock:
                record_click(conn, int(pid))
                conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
    if not valid:
        return redirect("/")
    return redirect(url, code=302)


@app.route("/api/owner/campaign/send", methods=["POST"])
def owner_campaign_send():
    """إرسال للمحدّدين من اللوحة: {ids:[...], channel:'whatsapp'|'email', offer:'report'|'newsite'}.

    يبني رسالة فرديّة (تقرير الموقع أو عرض موقع جديد + خدمات whats_bot)، يرسلها عبر القناة،
    ويحدّث حالة كل عميل إلى «أُرسل». يحترم الإرسال الفرديّ المهنيّ (PDPL).
    """
    if not is_owner():
        return jsonify({"error": "غير مصرّح"}), 403
    f = request.get_json(force=True, silent=True) or {}
    ids = f.get("ids") or []
    channel = (f.get("channel") or "whatsapp").lower()
    offer = (f.get("offer") or "report").lower()
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "لم تُحدَّد عملاء"}), 400
    if channel not in ("whatsapp", "email"):
        return jsonify({"error": "قناة غير مدعومة"}), 400

    wa_client = None
    if channel == "whatsapp":
        if not _wasender_key():
            return jsonify({"error": "مفتاح WaSenderAPI غير مضبوط على الخادم (WASENDER_API_KEY)"}), 400
        try:
            wa_client = WaSenderClient(min_interval=1.0, country="966")
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 400
    elif channel == "email" and not mailer.is_configured():
        return jsonify({"error": "البريد غير مُعدّ على الخادم (BOOKING_SMTP_*)"}), 400

    sent = failed = skipped = 0
    details = []
    conn = get_db()
    try:
        for raw in ids[:100]:
            if not str(raw).isdigit():
                continue
            pid = int(raw)
            p = _get_prospect(conn, pid)
            if not p:
                continue
            prospect = _row_to_dict_safe(p)
            # عرض «تقرير» لمن بلا موقع لا معنى له → حوّله لعرض موقع جديد تلقائياً
            eff_offer = offer
            if offer == "report" and not (prospect.get("website") or "").strip():
                eff_offer = "newsite"
            link = report_link_for(pid)
            message = build_message(prospect, eff_offer, link)

            if channel == "whatsapp":
                e164 = to_e164(prospect.get("whatsapp") or prospect.get("phone"), "966")
                if not e164:
                    skipped += 1
                    details.append({"id": pid, "skipped": "بلا واتساب"})
                    continue
                res = wa_client.send_text(e164, message)
            else:
                to = (prospect.get("email") or "").strip()
                if not to:
                    skipped += 1
                    details.append({"id": pid, "skipped": "بلا إيميل"})
                    continue
                pixel = f"{_public_base()}/api/track/open?id={pid}&ch=email"
                html_body = mailer.text_to_html(message, pixel_url=pixel, cta_url=link,
                                                cta_label="شاهد العرض")
                res = mailer.send_email(to, email_subject(prospect, eff_offer),
                                        html=html_body, text=message)

            if res.get("ok"):
                sent += 1
                with _book_lock:
                    mark_sent(conn, pid, message[:300], link)
                    conn.execute("UPDATE prospects SET channel=? WHERE id=?", (channel, pid))
                    conn.commit()
                details.append({"id": pid, "ok": True, "dry_run": res.get("dry_run", False)})
            else:
                failed += 1
                details.append({"id": pid, "error": res.get("error", "")[:100]})
    finally:
        conn.close()
    return jsonify({"ok": True, "sent": sent, "failed": failed, "skipped": skipped,
                    "channel": channel, "offer": offer, "details": details})


def _followup_message(prospect: dict, link: str, opened: bool) -> str:
    """رسالة متابعة لطيفة — أدفأ لمن فتح التقرير سابقًا."""
    name = prospect.get("name") or "منشأتكم"
    if opened:
        return (f"مرحباً {name} 🙏\n"
                f"لاحظنا اطّلاعكم على العرض — هل لديكم أي استفسار؟ يسعدنا تفعيله لكم "
                f"خلال يوم واحد (موقع + حجز واتساب).\n{link}")
    return (f"مرحباً {name} 👋\n"
            f"تذكير لطيف بعرضنا: موقع احترافيّ + حجز عبر واتساب يعمل ٢٤/٧.\n"
            f"يمكنكم الاطّلاع هنا: {link}\nمتى يناسبكم نشرحه باختصار؟")


@app.route("/api/owner/followup/run", methods=["POST"])
def owner_followup_run():
    """متابعة آليّة من اللوحة: تُرسل تذكيرًا لمن «أُرسل» لهم ولم يردّوا (واتساب).

    من ردّ (حالته ردّ/مهتم/عرض/عميل) لا تُتابَع. الجسم: {days:int=2} لتفادي التكرار اليوميّ.
    """
    if not is_owner():
        return jsonify({"error": "غير مصرّح"}), 403
    f = request.get_json(force=True, silent=True) or {}
    try:
        days = max(0, int(f.get("days", 2)))
    except (TypeError, ValueError):
        days = 2
    if not _wasender_key():
        return jsonify({"error": "مفتاح WaSenderAPI غير مضبوط على الخادم (WASENDER_API_KEY)"}), 400
    try:
        client = WaSenderClient(min_interval=1.0, country="966")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    sent = skipped = failed = 0
    conn = get_db()
    try:
        rows = list_prospects(conn, status="أُرسل")  # «ردّ» وما بعدها مستثناة تلقائيًّا
        for p in rows:
            lc = p.get("last_contacted_at") or ""
            if days > 0 and lc and lc > cutoff:      # تواصلنا حديثًا → تخطّى
                skipped += 1
                continue
            e164 = to_e164(p.get("whatsapp") or p.get("phone"), "966")
            if not e164:
                skipped += 1
                continue
            link = report_link_for(p["id"])
            msg = _followup_message(p, link, bool(p.get("opens")))
            res = client.send_text(e164, msg)
            if res.get("ok"):
                with _book_lock:
                    mark_sent(conn, p["id"], msg[:300], link)
                    conn.commit()
                sent += 1
            else:
                failed += 1
    finally:
        conn.close()
    return jsonify({"ok": True, "sent": sent, "skipped": skipped, "failed": failed})


@app.route("/owner/reports")
def owner_reports():
    if not is_owner():
        return redirect(url_for("owner_dash"))
    conn = get_db()
    try:
        prospects = list_prospects(conn)
    finally:
        conn.close()
    return Response(render_dashboard(compute_stats(prospects)), mimetype="text/html")


@app.route("/api/owner/prospect/<int:pid>/handoff", methods=["POST"])
def owner_prospect_handoff(pid):
    if not is_owner():
        return jsonify({"error": "غير مصرّح"}), 403
    conn = get_db()
    try:
        p = _get_prospect(conn, pid)
    finally:
        conn.close()
    if not p:
        return jsonify({"error": "غير موجود"}), 404
    return jsonify(handoff_prospect({"name": p["name"], "phone": p["phone"], "category": p["category"]}))


@app.route("/api/crm/record", methods=["POST"])
def crm_record():
    """تسجيل إرسال من send_campaign محلياً إلى الـCRM السحابي (مصادقة بترويسة X-Owner-Key)."""
    if not _verify_owner_pw((request.headers.get("X-Owner-Key") or "").strip()):
        return jsonify({"error": "غير مصرّح"}), 403
    d = request.get_json(force=True, silent=True) or {}
    if not isinstance(d, dict) or not d.get("name"):
        return jsonify({"error": "بيانات غير صالحة"}), 400
    intl = normalize_phone(d.get("phone"), "966")
    wa = intl if (intl and whatsappable(intl, d.get("phone"), "966")) else ""
    conn = get_db()
    try:
        with _book_lock:
            p = upsert_prospect(conn, {
                "feature_id": d.get("feature_id", ""), "name": d.get("name", ""),
                "phone": d.get("phone", ""), "whatsapp": wa, "category": d.get("category", ""),
                "website": d.get("website", ""), "source": d.get("source", "حملة"),
            })
            mark_sent(conn, p["id"], (d.get("message") or "أُرسلت رسالة")[:300], d.get("report_url"))
            conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "id": p.get("id")})


# بكسل شفّاف 1×1 لرصد فتح صفحة/تقرير العميل
_PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


@app.route("/api/track/open")
def track_open():
    """بكسل تتبّع عام: ?fid=<feature_id> أو ?id=<n> → يزيد عدّاد الفتح.

    ?ch=email يزيد عدّاد فتح الإيميل (email_opens) بدل فتح الصفحة (opens).
    """
    fid = (request.args.get("fid") or "").strip()
    pid = request.args.get("id")
    is_email = (request.args.get("ch") == "email")
    if fid or pid:
        key = int(pid) if (pid and pid.isdigit()) else fid
        conn = get_db()
        try:
            with _book_lock:
                if is_email:
                    record_email_open(conn, key)
                else:
                    record_open(conn, key)
                conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
    return Response(_PIXEL, mimetype="image/gif", headers={"Cache-Control": "no-store"})


def _jid_digits(jid: str) -> str:
    """يستخرج أرقام الهاتف من JID واتساب (9665xxxx@s.whatsapp.net) أو رقم عاديّ."""
    s = str(jid or "")
    s = s.split("@")[0].split(":")[0]
    return "".join(ch for ch in s if ch.isdigit())


def _match_prospect_id(conn, jid_or_phone: str):
    """يطابق رقماً واردًا (intl/national) بعميل في القاعدة. يُعيد id أو None."""
    d = _jid_digits(jid_or_phone)
    if not d:
        return None
    cands = {d, "+" + d}
    if d.startswith("966") and len(d) > 3:
        cands.add("0" + d[3:])
        cands.add(d[3:])
    cands = list(cands)
    ph = ",".join("?" * len(cands))
    try:
        row = conn.execute(
            f"SELECT id FROM prospects WHERE whatsapp IN ({ph}) OR phone IN ({ph}) "
            f"ORDER BY id LIMIT 1", tuple(cands) + tuple(cands)).fetchone()
        return row["id"] if row else None
    except Exception:
        return None


def _msg_text(msg: dict) -> str:
    """يستخرج نصّ رسالة واردة من بنى Baileys الشائعة (best-effort)."""
    m = (msg or {}).get("message") or {}
    if isinstance(m, dict):
        if m.get("conversation"):
            return str(m["conversation"])[:300]
        ext = m.get("extendedTextMessage") or {}
        if isinstance(ext, dict) and ext.get("text"):
            return str(ext["text"])[:300]
        for k in ("imageMessage", "videoMessage", "documentMessage"):
            cap = (m.get(k) or {}).get("caption") if isinstance(m.get(k), dict) else None
            if cap:
                return str(cap)[:300]
    return str(msg.get("text") or msg.get("body") or "")[:300]


def _parse_wasender_events(body: dict) -> list[dict]:
    """يحوّل حمولة webhook (WaSenderAPI/Baileys أو شكل مسطّح) إلى أحداث موحّدة.

    كل حدث: {phone, kind} حيث kind ∈ {reply, delivered, read}. متسامح مع عدّة صيَغ.
    """
    out: list[dict] = []
    if not isinstance(body, dict):
        return out
    event = str(body.get("event") or body.get("type") or "").lower()
    data = body.get("data", body)

    def _status_kind(status):
        s = str(status).lower()
        if s in ("3", "delivery_ack", "delivered", "server_ack_delivered"):
            return "delivered"
        if s in ("4", "5", "read", "played", "read_ack"):
            return "read"
        if s in ("2", "server_ack", "sent"):
            return "delivered"   # وصل للخادم — نعدّه تسليمًا تقريبيًّا
        return None

    # تمييز نوع الحدث بدقّة: تحديث حالة (update/ack/receipt) ≠ رسالة واردة (upsert/received)
    is_status_event = ("update" in event or "ack" in event or "receipt" in event)
    is_msg_event = (not is_status_event) and (
        "upsert" in event or "received" in event or event in ("message", "messages"))

    # 1) رسائل واردة (messages.upsert / received) — وليست تحديث حالة
    msgs = None
    if not is_status_event:
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            msgs = data["messages"]
        elif is_msg_event:
            if isinstance(data, list):
                msgs = data
            elif isinstance(data, dict) and data.get("key"):
                msgs = [data]
    if msgs:
        for msg in msgs:
            if not isinstance(msg, dict) or msg.get("update"):  # تجاهل عناصر تحديث الحالة
                continue
            key = msg.get("key") or {}
            from_me = key.get("fromMe", msg.get("fromMe", False))
            jid = key.get("remoteJid") or msg.get("from") or msg.get("remoteJid") or ""
            phone = _jid_digits(jid)
            if not phone:
                continue
            if not from_me:   # رسالة من العميل = ردّ
                out.append({"phone": phone, "kind": "reply", "text": _msg_text(msg)})

    # 2) تحديثات حالة (messages.update): تسليم/قراءة
    updates = None
    if is_status_event:
        if isinstance(data, list):
            updates = data
        elif isinstance(data, dict):
            updates = data.get("updates") if isinstance(data.get("updates"), list) else [data]
    if updates:
        for up in updates:
            if not isinstance(up, dict):
                continue
            key = up.get("key") or {}
            jid = (key.get("remoteJid") or up.get("remoteJid") or up.get("jid")
                   or up.get("to") or up.get("from") or "")
            phone = _jid_digits(jid)
            status = (up.get("update") or {}).get("status") if isinstance(up.get("update"), dict) else up.get("status")
            kind = _status_kind(status)
            # حدث message-receipt.update يحمل receipt بطوابع قراءة/تسليم بدل status
            if not kind:
                rc = up.get("receipt") or up.get("update") or {}
                if isinstance(rc, dict):
                    if rc.get("readTimestamp") or rc.get("readTimestampMs") or rc.get("read"):
                        kind = "read"
                    elif (rc.get("receiptTimestamp") or rc.get("deliveryTimestamp")
                          or rc.get("delivered")):
                        kind = "delivered"
            if phone and kind:
                out.append({"phone": phone, "kind": kind})

    # 3) شكل مسطّح بسيط: {from, message|status}
    if not out and isinstance(body, dict):
        phone = _jid_digits(body.get("from") or body.get("phone") or "")
        if phone:
            if body.get("message") or body.get("text"):
                out.append({"phone": phone, "kind": "reply", "text": _msg_text(body)})
            else:
                k = _status_kind(body.get("status"))
                if k:
                    out.append({"phone": phone, "kind": k})
    return out


def _verify_wasender_sig(raw_body: bytes) -> bool:
    """تحقّق متين من توقيع ويبهوك WaSenderAPI.

    يقبل أيًّا ممّا يلي مطابقًا لـ WASENDER_WEBHOOK_SECRET:
      • النصّ الخام في ترويسة X-Webhook-Signature أو X-Webhook-Secret أو معامل ?secret=
      • توقيع HMAC-SHA256 (hex، مع/بدون بادئة sha256=) للجسم الخام في X-Webhook-Signature
    إن لم يُضبط السرّ → يُقبل (مفتوح، لكن يُحدّث فقط أرقامًا موجودة).
    """
    secret = os.environ.get("WASENDER_WEBHOOK_SECRET", "").strip()
    if not secret:
        return True
    sig = (request.headers.get("X-Webhook-Signature") or "").strip()
    sec_hdr = (request.headers.get("X-Webhook-Secret") or "").strip()
    q = (request.args.get("secret") or "").strip()
    # 1) مطابقة نصّ خام (ترويسة أو معامل)
    for cand in (sig, sec_hdr, q):
        if cand and secrets.compare_digest(cand, secret):
            return True
    # 2) توقيع HMAC-SHA256 للجسم
    if sig:
        mac = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        s = sig.split("=", 1)[1] if sig.lower().startswith("sha256=") else sig
        if secrets.compare_digest(s.lower(), mac.lower()):
            return True
    return False


@app.route("/api/wasender/webhook", methods=["POST"])
def wasender_webhook():
    """يستقبل أحداث WaSenderAPI: تسليم/قراءة + كشف الردّ آليًّا → يحدّث الـCRM.

    الأمان: إن ضُبط WASENDER_WEBHOOK_SECRET يُتحقَّق من ترويسة X-Webhook-Signature
    (نصّ خام أو توقيع HMAC-SHA256 للجسم) أو ترويسة X-Webhook-Secret أو معامل ?secret=.
    يُحدّث فقط عملاء موجودين مطابقين بالرقم.
    إعداد الويبهوك في لوحة WaSenderAPI: Payload URL = {cloud}/api/wasender/webhook
    """
    raw = request.get_data() or b""
    if not _verify_wasender_sig(raw):
        return jsonify({"error": "توقيع غير صالح"}), 403
    body = request.get_json(force=True, silent=True) or {}
    events = _parse_wasender_events(body)
    replied = delivered = read = 0
    conn = get_db()
    try:
        for ev in events:
            pid = _match_prospect_id(conn, ev.get("phone", ""))
            if not pid:
                continue
            with _book_lock:
                if ev["kind"] == "reply":
                    record_reply(conn, pid, ev.get("text")); replied += 1
                elif ev["kind"] == "read":
                    mark_read(conn, pid); read += 1
                elif ev["kind"] == "delivered":
                    mark_delivered(conn, pid); delivered += 1
                conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "replied": replied, "delivered": delivered, "read": read,
                    "events": len(events)})


@app.route("/api/owner/prospects")
def api_owner_prospects():
    """قائمة العملاء JSON (لجلسة المالك أو لأداة المتابعة عبر X-Owner-Key)."""
    if not (is_owner() or _verify_owner_pw((request.headers.get("X-Owner-Key") or "").strip())):
        return jsonify({"error": "غير مصرّح"}), 403
    conn = get_db()
    try:
        rows = list_prospects(conn, status=request.args.get("status") or None,
                              q=request.args.get("q") or None)
    finally:
        conn.close()
    return jsonify({"prospects": rows})


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
  <h1>👑 لوحة المالك</h1>
  <div style="display:flex;gap:8px">
    <a class="btn p" href="/owner/crm">📇 المتابعة (CRM)</a>
    <a class="btn" style="background:#e7f7f0;color:#0c6b60" href="/owner/reports">📊 التقارير</a>
    <a class="btn" style="background:#eef1f4;color:#3b4a5a" href="/owner/logout">خروج</a>
  </div></div>
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


CRM_HTML = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>الاستهداف والمتابعة</title>
<style>""" + _BASE_CSS + """
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:14px}
.kpi{background:#fff;border:1px solid #e3e8ee;border-radius:14px;padding:12px;text-align:center}
.kpi b{display:block;font-size:22px;color:#128C7E}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#202124;color:#fff;padding:10px 18px;border-radius:10px;display:none;z-index:99}
select,input{padding:6px 8px;font-size:13px;width:auto}
.sc{display:inline-block;min-width:32px;text-align:center;padding:3px 8px;border-radius:20px;font-weight:700;color:#fff}
.sc.lo{background:#ea4335}.sc.mid{background:#fbbc04;color:#202124}.sc.hi{background:#34a853}.sc.na{background:#9aa0a6}
.act a,.act button{font-size:12px;padding:5px 8px;border-radius:7px;border:0;cursor:pointer;text-decoration:none;margin-inline-start:3px}
.act .au{background:#e7f7f0;color:#0c6b60}.act .wa{background:#25d366;color:#fff}.act .hb{background:#eef1f4;color:#3b4a5a}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.bar .btn{padding:8px 12px;font-size:13px}
.tgt{color:#f59e0b;font-weight:800}.rank{display:inline-block;background:#128C7E;color:#fff;border-radius:6px;padding:1px 7px;font-size:12px;font-weight:700}
.notes{max-width:230px;font-size:12px;color:#5b6b7b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.funnel{display:flex;gap:6px;flex-wrap:wrap;align-items:stretch;margin-bottom:6px}
.fstep{flex:1;min-width:90px;background:#fff;border:1px solid #e3e8ee;border-radius:12px;padding:10px;text-align:center;position:relative}
.fstep b{display:block;font-size:22px;color:#128C7E}.fstep span{font-size:12px;color:#7b8794}
.fstep .pc{font-size:11px;color:#9aa5b1}
.chips a{font-size:12px;padding:5px 10px;border-radius:20px;text-decoration:none;background:#eef1f4;color:#3b4a5a;margin-inline-start:4px}
.chips a.on{background:#128C7E;color:#fff}
td .mini{font-size:11px;color:#9aa5b1}
.selbar{position:sticky;top:0;z-index:10;background:#0c6b60;color:#fff;border-radius:12px;padding:10px 14px;display:none;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.selbar .btn{padding:7px 11px;font-size:13px}
.selbar select{color:#202124}
</style></head><body><div class="wrap">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
  <h1>🎯 الاستهداف والمتابعة</h1>
  <div style="display:flex;gap:8px"><a class="btn" style="background:#e7f7f0;color:#0c6b60" href="/owner/reports">📊 التقارير</a>
  <a class="btn" style="background:#eef1f4;color:#3b4a5a" href="/owner">← اللوحة</a></div></div>

<!-- القمع البصري -->
<div class="card" style="padding:14px"><h2 style="margin-bottom:8px">قمع التحويل</h2>
<div class="funnel">
  {% set t=funnel.get('total',0) or 1 %}
  <div class="fstep"><b>{{ funnel.get('total',0) }}</b><span>الإجمالي</span></div>
  <div class="fstep"><b>{{ funnel.get('targets',0) }}</b><span>مُستهدَف</span></div>
  <div class="fstep"><b>{{ funnel.get('sent',0) }}</b><span>أُرسل</span><div class="pc">{{ (100*funnel.get('sent',0)//t) }}%</div></div>
  <div class="fstep"><b>{{ funnel.get('opened',0) }}</b><span>فُتح</span><div class="pc">{{ (100*funnel.get('opened',0)//t) }}%</div></div>
  <div class="fstep"><b>{{ funnel.get('clicked',0) }}</b><span>نُقر الرابط</span><div class="pc">{{ (100*funnel.get('clicked',0)//t) }}%</div></div>
  <div class="fstep"><b>{{ funnel.get('replied',0) }}</b><span>ردّ/مهتم</span><div class="pc">{{ (100*funnel.get('replied',0)//t) }}%</div></div>
  <div class="fstep"><b>{{ funnel.get('customer',0) }}</b><span>عميل</span><div class="pc">{{ (100*funnel.get('customer',0)//t) }}%</div></div>
</div></div>

<div class="card" style="padding:14px"><h2>📥 استيراد عملاء محتملين</h2>
  <p class="muted" style="font-size:13px;margin-bottom:6px">ارفع ملف JSON من السحب — يُحفظون للاستهداف (بلا تكرار). أو احفظ مباشرةً من أداة السحب.</p>
  <input type="file" id="impf" accept=".json"><button class="btn p" onclick="doImport()">استيراد</button>
  <span id="impr" class="muted"></span>
</div>

<!-- شريط الاختيار الجماعي (يظهر عند تحديد صفوف) -->
<div class="selbar" id="selbar">
  <strong><span id="selcount">0</span> محدّد</strong>
  <button class="btn g" onclick="bulkAudit()">🔍 تدقيق المحدّدين</button>
  <button class="btn p" onclick="bulkTarget(true)">⭐ أضِف للاستهداف</button>
  <button class="btn" style="background:#fff3cd;color:#7a5b00" onclick="bulkTarget(false)">إزالة من الاستهداف</button>
  <span style="border-inline-start:1px solid #ffffff55;padding-inline-start:10px">إرسال:</span>
  <select id="channel"><option value="whatsapp">واتساب</option><option value="email">إيميل</option></select>
  <select id="offer"><option value="report">تقرير موقعهم</option><option value="newsite">عرض موقع جديد + whats_bot</option></select>
  <button class="btn g" onclick="bulkSend()">📤 إرسال للمحدّدين</button>
</div>

<!-- شريط الأدوات: الفرز/التصفية + إعادة الترتيب + المتابعة الآلية -->
<div class="bar">
  <span class="muted" style="font-size:13px">فرز:</span>
  <span class="chips">
    <a href="/owner/crm" class="{{ 'on' if not sort else '' }}">الأحدث</a>
    <a href="/owner/crm?sort=score" class="{{ 'on' if sort=='score' else '' }}">الأدنى درجةً (الأكثر حاجة)</a>
    <a href="/owner/crm?sort=rank{{ '&targets=1' if only_targets else '' }}" class="{{ 'on' if sort=='rank' else '' }}">ترتيب الاستهداف</a>
    <a href="/owner/crm?sort=opens" class="{{ 'on' if sort=='opens' else '' }}">الأكثر تفاعلاً</a>
    <a href="/owner/crm?targets=1{{ '&sort='+sort if sort else '' }}" class="{{ 'on' if only_targets else '' }}">⭐ المُستهدَفون فقط</a>
  </span>
  <span style="flex:1"></span>
  <button class="btn" style="background:#e7f7f0;color:#0c6b60" onclick="reRank()">↕ إعادة ترتيب الأولوية</button>
  <button class="btn" style="background:#eef1f4;color:#3b4a5a" onclick="runFollowup()">🔁 تشغيل المتابعة</button>
</div>

<div class="kpis">
  <div class="kpi"><b>{{ stats.values()|sum }}</b>الإجمالي</div>
  <div class="kpi"><b>{{ stats.get('أُرسل',0) }}</b>أُرسل لهم</div>
  <div class="kpi"><b>{{ stats.get('ردّ',0) }}</b>ردّوا</div>
  <div class="kpi"><b>{{ stats.get('مهتم',0) }}</b>مهتمّون</div>
  <div class="kpi"><b>{{ stats.get('عميل',0) }}</b>عملاء</div>
</div>

<div class="card"><h2>العملاء ({{ prospects|length }})</h2>
<table><thead><tr>
  <th style="width:28px"><input type="checkbox" id="all" onclick="toggleAll(this)" style="width:auto"></th>
  <th>المنشأة</th><th>الموقع</th><th>درجة</th><th>ملاحظات التدقيق</th><th>الحالة</th>
  <th>تفاعل</th><th>استهداف</th><th>إجراءات</th>
</tr></thead><tbody>
{% for p in prospects %}
<tr>
  <td><input type="checkbox" class="rowchk" value="{{ p['id'] }}" onclick="updSel()" style="width:auto"></td>
  <td><strong>{{ p['name'] }}</strong><div class="mini">{{ p['category'] }}{% if p['city'] %} · {{ p['city'] }}{% endif %}</div></td>
  <td>{% if p['website'] %}<a href="{{ p['website'] }}" target="_blank">موقع</a>{% else %}<span style="color:#ea4335">بلا موقع</span>{% endif %}</td>
  <td>{% set s=p['audit_score'] %}
    {% if s is none %}<span class="sc na">—</span>
    {% elif s>=70 %}<span class="sc hi">{{ s }}</span>
    {% elif s>=40 %}<span class="sc mid">{{ s }}</span>
    {% else %}<span class="sc lo">{{ s }}</span>{% endif %}</td>
  <td><div class="notes" title="{{ p['audit_issues'] or '' }}">{{ p['audit_issues'] or '—' }}</div></td>
  <td><select id="st{{ p['id'] }}" data-id="{{ p['id'] }}" onchange="onStatus(this)">
    {% for st in statuses %}<option value="{{ st }}" {{ 'selected' if p['status']==st else '' }}>{{ st }}</option>{% endfor %}
  </select><div class="mini">{{ (p['last_contacted_at'] or '')[:16] }}</div></td>
  <td>
    {% if p['opens'] %}<span style="color:#137333;font-weight:700" title="فتح الصفحة/التقرير">👁 {{ p['opens'] }}</span>{% endif %}
    {% if p['clicks'] %}<span style="color:#1a73e8;font-weight:700" title="نقر الرابط"> 🔗 {{ p['clicks'] }}</span>{% endif %}
    {% if p['read_at'] %}<span title="قرأ الواتساب">✓✓</span>{% endif %}
    {% if not p['opens'] and not p['clicks'] and not p['read_at'] %}<span class="muted">—</span>{% endif %}
  </td>
  <td>{% if p['is_target'] %}<span class="tgt" title="مُستهدَف">★</span>{% if p['target_rank'] %} <span class="rank">#{{ p['target_rank'] }}</span>{% endif %}{% else %}<span class="muted">—</span>{% endif %}</td>
  <td class="act">
    <a class="au" href="/owner/prospect/{{ p['id'] }}/audit" target="_blank">🔍</a>
    {% if p['whatsapp'] %}<a class="wa" href="https://wa.me/{{ p['whatsapp'] }}" target="_blank">واتساب</a>{% endif %}
    <button class="hb" onclick="handoff({{ p['id'] }})">whats_bot</button>
  </td>
</tr>
{% else %}<tr><td colspan="9" class="muted" style="text-align:center;padding:24px">لا يوجد عملاء بعد. احفظ من أداة السحب أو استورد ملف JSON بالأعلى.</td></tr>{% endfor %}
</tbody></table></div></div>
<div class="toast" id="toast"></div>
<script>
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2200);}
function selectedIds(){return Array.from(document.querySelectorAll('.rowchk:checked')).map(c=>parseInt(c.value));}
function updSel(){const n=selectedIds().length;document.getElementById('selcount').textContent=n;
  document.getElementById('selbar').style.display=n?'flex':'none';}
function toggleAll(cb){document.querySelectorAll('.rowchk').forEach(c=>c.checked=cb.checked);updSel();}
function need(ids){if(!ids.length){toast('حدّد عملاء أولاً');return false;}return true;}
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});return {ok:r.ok,d:await r.json().catch(()=>({}))};}
async function bulkAudit(){const ids=selectedIds();if(!need(ids))return;toast('جارٍ تدقيق '+ids.length+'…');
  const {ok,d}=await post('/api/owner/prospects/audit',{ids});
  toast(ok?('دُقّق '+d.audited+' · بلا موقع '+d.no_site+(d.failed?(' · فشل '+d.failed):'')):'خطأ');
  if(ok)setTimeout(()=>location.reload(),1100);}
async function bulkTarget(on){const ids=selectedIds();if(!need(ids))return;
  const {ok,d}=await post('/api/owner/prospects/target',{ids,on});
  toast(ok?((on?'أُضيف للاستهداف ':'أُزيل ')+d.updated):'خطأ');if(ok)setTimeout(()=>location.reload(),800);}
async function reRank(){const {ok,d}=await post('/api/owner/prospects/rank',{});toast(ok?('رُتّب '+d.ranked+' مُستهدَف'):'خطأ');if(ok)setTimeout(()=>location.reload(),800);}
async function bulkSend(){const ids=selectedIds();if(!need(ids))return;
  const channel=document.getElementById('channel').value, offer=document.getElementById('offer').value;
  if(!confirm('إرسال '+(offer==='report'?'تقرير الموقع':'عرض موقع جديد + whats_bot')+' عبر '+(channel==='email'?'الإيميل':'الواتساب')+' إلى '+ids.length+' عميلاً؟'))return;
  toast('جارٍ الإرسال…');
  const {ok,d}=await post('/api/owner/campaign/send',{ids,channel,offer});
  toast(ok?('أُرسل '+d.sent+(d.failed?(' · فشل '+d.failed):'')+(d.skipped?(' · تخطّي '+d.skipped):'')):('خطأ: '+(d.error||'')));
  if(ok)setTimeout(()=>location.reload(),1300);}
async function runFollowup(){if(!confirm('إرسال رسالة متابعة لمن أُرسل لهم ولم يردّوا (واتساب)؟'))return;toast('جارٍ المتابعة…');
  const {ok,d}=await post('/api/owner/followup/run',{});
  toast(ok?('تابعنا '+d.sent+' عميلاً'+(d.skipped?(' · تخطّي '+d.skipped):'')):('خطأ: '+(d.error||'')));
  if(ok)setTimeout(()=>location.reload(),1200);}
function setStatus(id,status,notes){post('/api/owner/prospect/'+id+'/status',{status,notes}).then(({d})=>toast(d.ok?'حُفظ ✓':('خطأ: '+(d.error||''))));}
function onStatus(sel){setStatus(sel.dataset.id,sel.value,null);}
function handoff(id){post('/api/owner/prospect/'+id+'/handoff',{}).then(({d})=>toast(d.note||d.action||'تم التسليم'));}
async function doImport(){
  const f=document.getElementById('impf').files[0]; if(!f){alert('اختر ملف JSON');return;}
  const fd=new FormData(); fd.append('file',f);
  document.getElementById('impr').textContent='جارٍ…';
  const r=await fetch('/api/owner/prospects/import',{method:'POST',body:fd}); const d=await r.json();
  document.getElementById('impr').textContent = r.ok ? ('أُضيف '+d.added+' — يُحدّث…') : ('خطأ: '+(d.error||''));
  if(r.ok) setTimeout(()=>location.reload(),900);
}
</script></body></html>"""


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
