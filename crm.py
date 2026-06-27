"""
طبقة بيانات «العملاء المحتملون / المتابعة» (CRM)
=================================================
تُدمج في قاعدة بيانات نظام الحجز (booking_system) سواء كانت SQLite محلية أو
Turso/libSQL سحابية. لا تستورد booking_system لتجنّب الاستيراد الدائري؛ بدلاً
من ذلك تستقبل كل دالة `conn` (اتصال get_db) كأول وسيط وتستخدم واجهته الموحّدة:

    conn.execute(sql, params).fetchone() / .fetchall() / .rowcount
    conn.commit() / conn.close()
    الصفوف تدعم الوصول بالاسم:  row["col"]

كل عبارات SQL مكتوبة بلهجة SQLite متوافقة مع libsql (CREATE TABLE IF NOT EXISTS،
علامات ? للمعاملات، لا دوال خاصة بمحرّك واحد).

الاستخدام النموذجي (من كود يملك اتصالاً عبر booking_system.get_db()):

    from crm import ensure_prospects_table, upsert_prospect, mark_sent, set_status
    conn = get_db()
    ensure_prospects_table(conn)
    upsert_prospect(conn, {"name": "مطعم النخبة", "feature_id": "0x..:0x..",
                           "phone": "+966500000000", "category": "مطعم", "city": "الرياض"})
    conn.commit(); conn.close()
"""

from __future__ import annotations

import sys
from datetime import datetime

# على Windows: أجبر UTF-8 لتجنّب UnicodeEncodeError عند طباعة العربية
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# الحالات المعتمدة لمسار المتابعة                                              #
# --------------------------------------------------------------------------- #
STATUSES = ["جديد", "أُرسل", "ردّ", "مهتم", "عرض", "عميل", "مرفوض", "موقوف"]

# كلمات طلب التوقّف (opt-out) — أي ردّ يحويها يُوقف التواصل فورًا (امتثال PDPL + حماية الرقم)
STOP_WORDS = (
    "إيقاف", "ايقاف", "أوقف", "اوقف", "توقف", "قف", "إلغاء", "الغاء", "ألغ", "الغ",
    "لا ترسل", "لا تراسل", "لا تراسلني", "بدون رسائل", "ازالة", "إزالة", "احذف",
    "stop", "unsubscribe", "remove", "cancel", "opt out", "optout",
)


def is_stop_message(text) -> bool:
    """هل الرسالة طلب توقّف صريح؟ (مطابقة جزئية متسامحة)."""
    if not text:
        return False
    t = str(text).strip().lower()
    return any(w.lower() in t for w in STOP_WORDS)


# --------------------------------------------------------------------------- #
# أدوات داخلية                                                                 #
# --------------------------------------------------------------------------- #
def _now() -> str:
    """طابع زمني محلي بدقّة الثانية (ISO 8601)."""
    return datetime.now().isoformat(timespec="seconds")


# الأعمدة القابلة للكتابة من قِبل المستخدم عند الإدراج/التحديث
_PROSPECT_FIELDS = (
    "feature_id", "name", "phone", "whatsapp", "email", "category", "city", "source",
    "website", "audit_score", "audit_issues", "audit_strengths", "status",
    "last_message", "report_url", "notes", "last_contacted_at",
    "is_target", "target_rank", "channel",
)

# أعمدة إضافية تُدار عبر دوال مخصّصة (تتبّع/تدقيق/استهداف) — تُضاف بالترحيل إن غابت.
# (name -> SQL declaration)
_EXTRA_COLUMNS = {
    "email": "TEXT",
    "audit_issues": "TEXT",
    "audit_strengths": "TEXT",
    "audit_dims": "TEXT",
    "is_target": "INTEGER DEFAULT 0",
    "target_rank": "INTEGER",
    "channel": "TEXT",
    "opens": "INTEGER DEFAULT 0",
    "last_open_at": "TEXT",
    "clicks": "INTEGER DEFAULT 0",
    "last_click_at": "TEXT",
    "delivered_at": "TEXT",
    "read_at": "TEXT",
    "replied_at": "TEXT",
    "email_opens": "INTEGER DEFAULT 0",
    "last_email_open_at": "TEXT",
}


def _row_to_dict(row) -> dict | None:
    """يحوّل صفّاً (sqlite3.Row أو صف libsql) إلى dict عادي.

    يعمل مع:
      • sqlite3.Row     → يدعم .keys() و row[key]
      • صفوف libsql     → تدعم .keys() أيضاً (أو الوصول بالاسم/الفهرس)
    """
    if row is None:
        return None
    try:
        keys = list(row.keys())
        return {k: row[k] for k in keys}
    except Exception:
        # احتياط: صف يشبه التسلسل بلا keys() — غير متوقّع مع backendينا، لكن آمن
        return dict(row)


# --------------------------------------------------------------------------- #
# تهيئة الجدول                                                                 #
# --------------------------------------------------------------------------- #
def ensure_prospects_table(conn) -> None:
    """ينشئ جدول prospects وفهارسه إن لم تكن موجودة (آمن للتكرار)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS prospects ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " feature_id TEXT,"
        " name TEXT,"
        " phone TEXT,"
        " whatsapp TEXT,"
        " email TEXT,"
        " category TEXT,"
        " city TEXT,"
        " source TEXT,"
        " website TEXT,"
        " audit_score INTEGER,"
        " audit_issues TEXT,"
        " audit_strengths TEXT,"
        " audit_dims TEXT,"
        " status TEXT DEFAULT 'جديد',"
        " last_message TEXT,"
        " report_url TEXT,"
        " notes TEXT,"
        " last_contacted_at TEXT,"
        " is_target INTEGER DEFAULT 0,"
        " target_rank INTEGER,"
        " channel TEXT,"
        " opens INTEGER DEFAULT 0,"
        " last_open_at TEXT,"
        " clicks INTEGER DEFAULT 0,"
        " last_click_at TEXT,"
        " delivered_at TEXT,"
        " read_at TEXT,"
        " replied_at TEXT,"
        " email_opens INTEGER DEFAULT 0,"
        " last_email_open_at TEXT,"
        " created_at TEXT,"
        " updated_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prospects_feature ON prospects(feature_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status)"
    )
    # ترحيل الجداول القديمة: أضِف أي عمود حديث غائب (تتبّع/تدقيق/استهداف/إيميل)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(prospects)").fetchall()}
        for col, decl in _EXTRA_COLUMNS.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE prospects ADD COLUMN {col} {decl}")
    except Exception:
        pass


def record_open(conn, key) -> bool:
    """يزيد عدّاد فتح التقرير/الصفحة لعميل (مطابقة بـ id أو feature_id أو phone)."""
    now = _now()
    target_id = None
    if isinstance(key, int):
        row = conn.execute("SELECT id FROM prospects WHERE id=? LIMIT 1", (key,)).fetchone()
        if row is not None:
            target_id = row["id"]
    else:
        skey = (str(key).strip() if key is not None else "") or None
        ex = _find_existing(conn, skey, skey)
        if ex is not None:
            target_id = ex["id"]
    if target_id is None:
        return False
    conn.execute(
        "UPDATE prospects SET opens=COALESCE(opens,0)+1, last_open_at=?, updated_at=? WHERE id=?",
        (now, now, target_id),
    )
    return True


# --------------------------------------------------------------------------- #
# محلّل المفتاح المشترك (id / feature_id / phone)                              #
# --------------------------------------------------------------------------- #
def _resolve_id(conn, key):
    """يحوّل مفتاحاً (id رقمي أو feature_id/phone نصّي) إلى id السجل، أو None."""
    if key is None:
        return None
    if isinstance(key, int):
        row = conn.execute("SELECT id FROM prospects WHERE id=? LIMIT 1", (key,)).fetchone()
        return row["id"] if row is not None else None
    skey = str(key).strip() or None
    if skey is None:
        return None
    if skey.isdigit():
        row = conn.execute("SELECT id FROM prospects WHERE id=? LIMIT 1", (int(skey),)).fetchone()
        if row is not None:
            return row["id"]
    ex = _find_existing(conn, skey, skey)
    return ex["id"] if ex is not None else None


# --------------------------------------------------------------------------- #
# حفظ نتيجة تدقيق الموقع (الدرجة + الملاحظات النصية + نقاط القوة)               #
# --------------------------------------------------------------------------- #
def save_audit(conn, prospect_id, audit: dict) -> bool:
    """يخزّن نتيجة audit_site لعميل: الدرجة، ونصّ المشاكل، ونصّ نقاط القوة.

    `audit` هو ناتج audit.audit_site (فيه score/issues/strengths). يُعيد True عند النجاح.
    """
    pid = _resolve_id(conn, prospect_id)
    if pid is None:
        return False
    audit = audit or {}
    score = audit.get("score")
    issues = audit.get("issues") or []
    strengths = audit.get("strengths") or []
    issues_text = " • ".join(str(x) for x in issues)[:2000] if issues else ""
    strengths_text = " • ".join(str(x) for x in strengths)[:2000] if strengths else ""
    # نخزّن صورة كاملة للتدقيق (JSON) ليُعرض التقرير لاحقًا بلا أي جلب شبكيّ حيّ
    import json as _json
    snapshot = {
        "url": audit.get("url", ""), "score": score, "dims": audit.get("dims", {}),
        "issues": list(issues), "strengths": list(strengths),
        "has_booking": audit.get("has_booking"), "has_whatsapp": audit.get("has_whatsapp"),
        "https": audit.get("https"), "mobile": audit.get("mobile"),
    }
    try:
        dims_json = _json.dumps(snapshot, ensure_ascii=False)[:8000]
    except Exception:
        dims_json = ""
    now = _now()
    conn.execute(
        "UPDATE prospects SET audit_score=?, audit_issues=?, audit_strengths=?, audit_dims=?, updated_at=? WHERE id=?",
        (score, issues_text, strengths_text, dims_json, now, pid),
    )
    return True


# --------------------------------------------------------------------------- #
# الاستهداف: تعليم/إلغاء «مُستهدَف» + ترتيب الأولوية                            #
# --------------------------------------------------------------------------- #
def set_target(conn, ids, on: bool = True) -> int:
    """يضبط علم is_target لقائمة معرّفات. يُعيد عدد الصفوف المتأثّرة."""
    if isinstance(ids, (int, str)):
        ids = [ids]
    now = _now()
    n = 0
    for key in ids:
        pid = _resolve_id(conn, key)
        if pid is None:
            continue
        conn.execute(
            "UPDATE prospects SET is_target=?, updated_at=? WHERE id=?",
            (1 if on else 0, now, pid),
        )
        n += 1
    return n


def rank_targets(conn) -> int:
    """يرتّب العملاء المُستهدَفين بالأولوية ويكتب target_rank (1 = الأعلى أولوية).

    الأولوية: الأكثر حاجةً أولاً = (بلا موقع) ثم أدنى درجة تدقيق، ثم الأكثر مراجعات
    (سمعة قوية غير مستثمَرة). نُرتّب ونكتب رقماً تسلسلياً. يُعيد عدد المُستهدَفين.
    """
    rows = conn.execute(
        "SELECT id, audit_score, website FROM prospects WHERE is_target=1"
    ).fetchall()
    items = [_row_to_dict(r) for r in rows]

    def _key(p):
        has_site = 1 if (p.get("website") or "").strip() else 0
        score = p.get("audit_score")
        score = 999 if score is None else score   # غير مُدقّق → آخر الأولوية حتى يُدقّق
        return (has_site, score)

    items.sort(key=_key)
    now = _now()
    for i, p in enumerate(items, 1):
        conn.execute(
            "UPDATE prospects SET target_rank=?, updated_at=? WHERE id=?",
            (i, now, p["id"]),
        )
    return len(items)


# --------------------------------------------------------------------------- #
# تتبّع التفاعل: نقر الرابط، فتح الإيميل، التسليم/القراءة، الردّ                #
# --------------------------------------------------------------------------- #
def record_click(conn, key) -> bool:
    """يزيد عدّاد نقر الرابط لعميل."""
    pid = _resolve_id(conn, key)
    if pid is None:
        return False
    now = _now()
    conn.execute(
        "UPDATE prospects SET clicks=COALESCE(clicks,0)+1, last_click_at=?, updated_at=? WHERE id=?",
        (now, now, pid),
    )
    return True


def record_email_open(conn, key) -> bool:
    """يزيد عدّاد فتح الإيميل لعميل."""
    pid = _resolve_id(conn, key)
    if pid is None:
        return False
    now = _now()
    conn.execute(
        "UPDATE prospects SET email_opens=COALESCE(email_opens,0)+1, last_email_open_at=?, updated_at=? WHERE id=?",
        (now, now, pid),
    )
    return True


def mark_delivered(conn, key) -> bool:
    """يسجّل وقت تسليم الرسالة (من webhook المزوّد)."""
    pid = _resolve_id(conn, key)
    if pid is None:
        return False
    now = _now()
    conn.execute("UPDATE prospects SET delivered_at=?, updated_at=? WHERE id=?", (now, now, pid))
    return True


def mark_read(conn, key) -> bool:
    """يسجّل وقت قراءة الرسالة (من webhook المزوّد)."""
    pid = _resolve_id(conn, key)
    if pid is None:
        return False
    now = _now()
    conn.execute("UPDATE prospects SET read_at=?, updated_at=? WHERE id=?", (now, now, pid))
    return True


def record_reply(conn, key, message: str | None = None) -> bool:
    """يسجّل ردّ العميل: يضبط replied_at، ويرقّي الحالة إلى «ردّ» (إن لم تكن متقدّمة).

    إن كان الردّ طلب توقّف (إيقاف/stop/…) يُضبط إلى «موقوف» فورًا (opt-out إلزاميّ).
    لا يُنزل حالة متقدّمة (مهتم/عرض/عميل) إلى «ردّ» — يحترم التقدّم اليدوي.
    """
    pid = _resolve_id(conn, key)
    if pid is None:
        return False
    now = _now()
    row = conn.execute("SELECT status FROM prospects WHERE id=? LIMIT 1", (pid,)).fetchone()
    cur_status = (row["status"] if row is not None else None) or "جديد"

    if is_stop_message(message):
        # طلب توقّف صريح: أوقِف التواصل بغضّ النظر عن الحالة الحالية
        new_status = "موقوف"
    else:
        advanced = ("مهتم", "عرض", "عميل")
        new_status = cur_status if cur_status in advanced else "ردّ"

    if message:
        conn.execute(
            "UPDATE prospects SET status=?, replied_at=?, last_message=?, updated_at=? WHERE id=?",
            (new_status, now, str(message)[:300], now, pid),
        )
    else:
        conn.execute(
            "UPDATE prospects SET status=?, replied_at=?, updated_at=? WHERE id=?",
            (new_status, now, now, pid),
        )
    return True


# --------------------------------------------------------------------------- #
# البحث عن سجل موجود                                                           #
# --------------------------------------------------------------------------- #
def _find_existing(conn, feature_id: str | None, phone: str | None):
    """يبحث عن سجل مطابق: أولاً بـ feature_id (إن وُجد)، وإلا بـ phone."""
    if feature_id:
        row = conn.execute(
            "SELECT * FROM prospects WHERE feature_id=? LIMIT 1", (feature_id,)
        ).fetchone()
        if row is not None:
            return row
    if phone:
        row = conn.execute(
            "SELECT * FROM prospects WHERE phone=? LIMIT 1", (phone,)
        ).fetchone()
        if row is not None:
            return row
    return None


def _get_by_id(conn, prospect_id):
    row = conn.execute(
        "SELECT * FROM prospects WHERE id=? LIMIT 1", (prospect_id,)
    ).fetchone()
    return _row_to_dict(row)


# --------------------------------------------------------------------------- #
# إدراج/تحديث (Upsert)                                                         #
# --------------------------------------------------------------------------- #
def upsert_prospect(conn, data: dict) -> dict:
    """يُدرج عميلاً محتملاً جديداً أو يُحدّث المطابق له.

    المطابقة على feature_id (إن وُجد) وإلا على phone. عند التحديث تُكتب فقط
    الحقول الموجودة في `data` (لا تُمحى الحقول غير المذكورة). تُضبط الطوابع
    الزمنية تلقائياً: created_at عند الإدراج، updated_at دائماً.

    يُعيد السجل المخزّن كـ dict (بما فيه id والطوابع).
    """
    data = data or {}
    feature_id = (data.get("feature_id") or "").strip() or None
    phone = (data.get("phone") or "").strip() or None
    existing = _find_existing(conn, feature_id, phone)
    now = _now()

    if existing is not None:
        existing_id = existing["id"]
        # حدّث الحقول الموجودة في data فقط
        sets = []
        params = []
        for field in _PROSPECT_FIELDS:
            if field in data:
                sets.append(f"{field}=?")
                params.append(data[field])
        sets.append("updated_at=?")
        params.append(now)
        params.append(existing_id)
        conn.execute(
            f"UPDATE prospects SET {', '.join(sets)} WHERE id=?", tuple(params)
        )
        return _get_by_id(conn, existing_id)

    # إدراج جديد
    cols = []
    placeholders = []
    params = []
    for field in _PROSPECT_FIELDS:
        if field in data:
            cols.append(field)
            placeholders.append("?")
            params.append(data[field])
    # تأكّد من ضبط الحالة الافتراضية إن لم تُمرَّر
    if "status" not in data:
        cols.append("status")
        placeholders.append("?")
        params.append("جديد")
    cols.extend(["created_at", "updated_at"])
    placeholders.extend(["?", "?"])
    params.extend([now, now])

    cur = conn.execute(
        f"INSERT INTO prospects ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
        tuple(params),
    )
    new_id = getattr(cur, "lastrowid", None)
    if not new_id:
        # بعض الواجهات (مثل libsql عبر بعض النقل) تُعيد lastrowid=0/None — استرجع بالمفتاح
        fetched = _find_existing(conn, feature_id, phone)
        if fetched is not None:
            return _row_to_dict(fetched)
        # أو بآخر سجلّ مُدرَج (احتياط أخير)
        last = conn.execute("SELECT * FROM prospects ORDER BY id DESC LIMIT 1").fetchone()
        return _row_to_dict(last)
    return _get_by_id(conn, new_id)


# --------------------------------------------------------------------------- #
# تعليم «أُرسل» بعد إرسال رسالة                                                #
# --------------------------------------------------------------------------- #
def mark_sent(conn, key, message, report_url=None):
    """يضبط حالة العميل المحتمل إلى «أُرسل» ويسجّل آخر رسالة ووقت التواصل.

    `key` يمكن أن يكون:
      • رقم id (int) لسجل موجود، أو
      • feature_id (str) أو phone (str) لمطابقة سجل موجود.

    يُحدّث: status='أُرسل'، last_contacted_at=now، last_message=message،
    و report_url إن مُرّر. يُعيد السجل المُحدّث (dict) أو None إن لم يُطابق شيء.
    """
    now = _now()
    target_id = None

    if isinstance(key, int):
        row = conn.execute(
            "SELECT id FROM prospects WHERE id=? LIMIT 1", (key,)
        ).fetchone()
        if row is not None:
            target_id = row["id"]
    else:
        skey = (str(key).strip() if key is not None else "") or None
        existing = _find_existing(conn, skey, skey)
        if existing is not None:
            target_id = existing["id"]

    if target_id is None:
        return None

    if report_url is not None:
        conn.execute(
            "UPDATE prospects SET status=?, last_contacted_at=?, last_message=?, "
            "report_url=?, updated_at=? WHERE id=?",
            ("أُرسل", now, message, report_url, now, target_id),
        )
    else:
        conn.execute(
            "UPDATE prospects SET status=?, last_contacted_at=?, last_message=?, "
            "updated_at=? WHERE id=?",
            ("أُرسل", now, message, now, target_id),
        )
    return _get_by_id(conn, target_id)


# --------------------------------------------------------------------------- #
# تغيير الحالة يدوياً                                                          #
# --------------------------------------------------------------------------- #
def set_status(conn, prospect_id, status, notes=None) -> bool:
    """يضبط حالة سجل (مع التحقّق من أنها ضمن STATUSES).

    إن مُرّرت `notes` تُحدَّث أيضاً. يُعيد True إن تأثّر صفّ واحد على الأقل.
    """
    if status not in STATUSES:
        return False
    now = _now()
    if notes is not None:
        cur = conn.execute(
            "UPDATE prospects SET status=?, notes=?, updated_at=? WHERE id=?",
            (status, notes, now, prospect_id),
        )
    else:
        cur = conn.execute(
            "UPDATE prospects SET status=?, updated_at=? WHERE id=?",
            (status, now, prospect_id),
        )
    return getattr(cur, "rowcount", 0) > 0


# --------------------------------------------------------------------------- #
# الاستعلام                                                                    #
# --------------------------------------------------------------------------- #
def list_prospects(conn, status=None, q=None, sort=None, only_targets=False) -> list[dict]:
    """يُعيد قائمة العملاء المحتملين، مع فلترة اختيارية بالحالة و/أو نص بحث.

    `q` يبحث في name و phone و category و city (تطابق جزئي LIKE).
    `sort`: None=الأحدث (افتراضي)، "score"=الأدنى درجةً أولاً (الأكثر حاجة)،
            "rank"=ترتيب الاستهداف، "opens"=الأكثر تفاعلاً.
    `only_targets`: True يقصر النتيجة على المُستهدَفين (is_target=1).
    """
    where = []
    params: list = []
    if status:
        where.append("status=?")
        params.append(status)
    if only_targets:
        where.append("is_target=1")
    if q:
        like = f"%{q}%"
        where.append(
            "(name LIKE ? OR phone LIKE ? OR category LIKE ? OR city LIKE ?)"
        )
        params.extend([like, like, like, like])
    sql = "SELECT * FROM prospects"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if sort == "score":
        # الأدنى درجةً أولاً (الأكثر حاجة)؛ غير المُدقّق (NULL) في النهاية
        sql += " ORDER BY (audit_score IS NULL), audit_score ASC, id DESC"
    elif sort == "rank":
        sql += " ORDER BY (target_rank IS NULL), target_rank ASC, id DESC"
    elif sort == "opens":
        sql += " ORDER BY COALESCE(opens,0)+COALESCE(clicks,0) DESC, id DESC"
    else:
        sql += " ORDER BY (updated_at IS NULL), updated_at DESC, id DESC"
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_dict(r) for r in rows]


def stats_by_status(conn) -> dict:
    """يُعيد عدد السجلات لكل حالة كقاموس {الحالة: العدد}.

    يضمن وجود كل حالة من STATUSES في الناتج (بقيمة 0 إن لم توجد سجلات)،
    وأي حالات أخرى غير قياسية موجودة في البيانات تُضاف كذلك.
    """
    out = {s: 0 for s in STATUSES}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM prospects GROUP BY status"
    ).fetchall()
    for r in rows:
        st = r["status"]
        cnt = r["c"]
        if st is None:
            st = "جديد"
        out[st] = out.get(st, 0) + (cnt or 0)
    return out


# --------------------------------------------------------------------------- #
# اختبار ذاتي يثبت العقد                                                       #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # 1) تهيئة الجدول (مرتين للتأكّد من أمان التكرار)
    ensure_prospects_table(conn)
    ensure_prospects_table(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(prospects)").fetchall()}
    expected_cols = {
        "id", "feature_id", "name", "phone", "whatsapp", "category", "city",
        "source", "website", "audit_score", "status", "last_message",
        "report_url", "notes", "last_contacted_at", "created_at", "updated_at",
    }
    assert expected_cols <= cols, f"أعمدة ناقصة: {expected_cols - cols}"

    # 2) upsert مرتين لنفس feature_id → سجل واحد فقط (تحديث لا إدراج)
    fid = "0x3e2f:0xabc123"
    p1 = upsert_prospect(conn, {
        "feature_id": fid, "name": "مطعم النخبة", "phone": "+966500000001",
        "category": "مطعم", "city": "الرياض", "source": "google_maps",
        "audit_score": 42,
    })
    assert p1["id"] is not None, "الإدراج لم يُعِد id"
    assert p1["status"] == "جديد", f"الحالة الافتراضية يجب أن تكون 'جديد' لا {p1['status']!r}"
    assert p1["created_at"] and p1["updated_at"], "الطوابع لم تُضبط عند الإدراج"

    p2 = upsert_prospect(conn, {
        "feature_id": fid, "name": "مطعم النخبة (محدّث)", "audit_score": 55,
    })
    assert p2["id"] == p1["id"], "upsert لنفس feature_id يجب أن يُحدّث لا يُدرج"
    assert p2["name"] == "مطعم النخبة (محدّث)", "الاسم لم يُحدّث"
    assert p2["audit_score"] == 55, "audit_score لم يُحدّث"
    # الحقول غير المذكورة في التحديث تبقى كما هي
    assert p2["city"] == "الرياض", "الحقول غير المذكورة يجب أن تبقى"
    assert p2["phone"] == "+966500000001", "الهاتف يجب أن يبقى بعد التحديث"

    cnt = conn.execute("SELECT COUNT(*) AS c FROM prospects").fetchone()["c"]
    assert cnt == 1, f"يجب أن يكون هناك سجل واحد فقط، وُجد {cnt}"

    # 3) upsert لسجل جديد بلا feature_id يطابق على phone
    pa = upsert_prospect(conn, {
        "name": "عيادة الشفاء", "phone": "+966500000002", "category": "عيادة",
    })
    pb = upsert_prospect(conn, {"phone": "+966500000002", "city": "جدة"})
    assert pa["id"] == pb["id"], "upsert على نفس phone يجب أن يُطابق"
    assert pb["city"] == "جدة", "تحديث المطابقة على phone لم يكتب city"
    cnt = conn.execute("SELECT COUNT(*) AS c FROM prospects").fetchone()["c"]
    assert cnt == 2, f"يجب أن يكون هناك سجلان، وُجد {cnt}"

    # 4) mark_sent عبر feature_id ثم عبر id
    sent = mark_sent(conn, fid, "السلام عليكم، نعرض صفحة حجز إلكتروني.",
                     report_url="https://example.com/r/1")
    assert sent is not None, "mark_sent لم يجد السجل عبر feature_id"
    assert sent["status"] == "أُرسل", "الحالة لم تتحوّل إلى 'أُرسل'"
    assert sent["last_message"], "last_message لم يُسجَّل"
    assert sent["report_url"] == "https://example.com/r/1", "report_url لم يُحفظ"
    assert sent["last_contacted_at"], "last_contacted_at لم يُضبط"

    sent_by_id = mark_sent(conn, pa["id"], "رسالة متابعة ثانية")
    assert sent_by_id is not None and sent_by_id["status"] == "أُرسل", "mark_sent عبر id فشل"

    assert mark_sent(conn, 999999, "لا يوجد") is None, "mark_sent لمفتاح غير موجود يجب أن يُعيد None"

    # 5) set_status بحالة صالحة وأخرى غير صالحة
    ok = set_status(conn, p1["id"], "مهتم", notes="طلب عرض سعر")
    assert ok is True, "set_status بحالة صالحة يجب أن يُعيد True"
    after = _get_by_id(conn, p1["id"])
    assert after["status"] == "مهتم", "الحالة لم تُحدّث"
    assert after["notes"] == "طلب عرض سعر", "الملاحظات لم تُحدّث"

    bad = set_status(conn, p1["id"], "حالة-وهمية")
    assert bad is False, "set_status بحالة غير صالحة يجب أن يُعيد False"

    missing = set_status(conn, 999999, "عميل")
    assert missing is False, "set_status لسجل غير موجود يجب أن يُعيد False"

    # 6) list_prospects: بلا فلترة، بحالة، وبنص بحث
    all_rows = list_prospects(conn)
    assert len(all_rows) == 2, f"list_prospects يجب أن يُعيد سجلين، أعاد {len(all_rows)}"

    by_status = list_prospects(conn, status="مهتم")
    assert len(by_status) == 1 and by_status[0]["status"] == "مهتم", "فلترة الحالة فشلت"

    by_q = list_prospects(conn, q="عيادة")
    assert len(by_q) == 1 and by_q[0]["category"] == "عيادة", "بحث النص فشل"

    by_city = list_prospects(conn, q="جدة")
    assert len(by_city) == 1, "البحث في city فشل"

    # 7) stats_by_status: يجب أن يحوي كل الحالات، والمجموع = عدد السجلات
    stats = stats_by_status(conn)
    for s in STATUSES:
        assert s in stats, f"الحالة {s} مفقودة من الإحصاءات"
    assert stats["مهتم"] == 1, "إحصاء 'مهتم' يجب أن يكون 1"
    assert stats["أُرسل"] == 1, "إحصاء 'أُرسل' يجب أن يكون 1 (عيادة الشفاء)"
    assert sum(stats.values()) == 2, f"مجموع الإحصاءات يجب أن يساوي 2، وجد {sum(stats.values())}"

    conn.commit()
    conn.close()
    print("PASS: crm.py — كل اختبارات العقد نجحت")
