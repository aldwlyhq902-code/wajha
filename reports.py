"""
تقارير الأداء (Reports) — حملة التواصل + الـCRM.
================================================
وحدة نقيّة (stdlib فقط) تحوّل قائمة الفرص/العملاء المحتملين (prospects) إلى:

  • compute_stats(prospects)   : إحصاءات شاملة (إجمالي، حسب الحالة/الفئة/المدينة،
                                  معدل التحويل، نسبة الردّ، شرائح درجة التدقيق،
                                  وعدد من بلا موقع).
  • render_dashboard(stats)    : لوحة HTML عربية RTL مسطّحة بهوية «واجهة»
                                  (بطاقات KPI + أعمدة CSS بسيطة للتوزيع).

العقد ثابت ومستقرّ — تُستهلك من لوحة booking_system أو أي مُشغّل خارجي.

البنية المتوقّعة لكل عنصر prospect (كل الحقول اختيارية ومتسامحة):
    {
      "name": str, "category": str, "city": str,
      "status": str,          # new/sent/replied/interested/customer/lost ...
      "audit_score": int|str, # درجة تدقيق الموقع 0..100
      "website": str,         # فارغ/مفقود => «بلا موقع»
    }

التشغيل المباشر يبني ~12 عميلاً وهمياً، يطبع الإحصاءات، ويولّد اللوحة للتحقّق.
"""

from __future__ import annotations

import html
import sys
from datetime import datetime

# على Windows: أجبر UTF-8 لتجنّب UnicodeEncodeError عند طباعة العربية
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# مفردات الحالة (CRM) — ترتيب القمع من البداية حتى التحويل                      #
# --------------------------------------------------------------------------- #
# ملاحظة مهمّة: قاعدة crm.py تخزّن الحالات بالعربية (جديد/أُرسل/ردّ/مهتم/عرض/عميل/...)،
# لذا نُطبّع هنا الحالة إلى «رمز قانونيّ» يقبل الشكلين العربيّ والإنجليزيّ معاً.
# (كان هذا خللاً سابقاً: التُّعرَّف بالإنجليزية فقط فتُحسب المعدّلات صفراً على البيانات الحقيقية.)
_STATUS_CANON = {
    # عربي (المصدر الفعليّ)
    "جديد": "new", "بالانتظار": "queued", "أُرسل": "sent", "ارسل": "sent",
    "ردّ": "replied", "رد": "replied", "مهتم": "interested", "عرض": "offer",
    "عميل": "customer", "مرفوض": "lost", "موقوف": "paused",
    # إنجليزي (توافق خلفيّ)
    "new": "new", "queued": "queued", "sent": "sent", "replied": "replied",
    "interested": "interested", "negotiating": "interested", "offer": "offer",
    "customer": "customer", "lost": "lost", "unqualified": "lost", "paused": "paused",
}

# الحالات «المُرسَلة» = كل ما تجاوز مرحلة الإضافة الأوليّة (دخل القمع فعلياً).
_SENT_STATUSES = ("sent", "replied", "interested", "offer", "customer")
# الحالات «المتجاوِبة» = من ردّ بأي شكل.
_REPLIED_STATUSES = ("replied", "interested", "offer", "customer")
# الحالات «المهتمّة».
_INTERESTED_STATUSES = ("interested", "offer")
# الحالة «عميل» (تحوّل ناجح).
_CUSTOMER_STATUSES = ("customer",)

# تسميات عربية للحالات الشائعة (لعرضها في اللوحة) — مفاتيحها الرموز القانونية.
STATUS_LABELS = {
    "new":         "جديد",
    "queued":      "بالانتظار",
    "sent":        "أُرسل",
    "replied":     "ردّ",
    "interested":  "مهتم",
    "offer":       "عرض",
    "customer":    "عميل",
    "lost":        "مرفوض",
    "paused":      "موقوف",
    "":            "غير محدد",
}

# شرائح درجة التدقيق (audit_score 0..100).
_SCORE_BANDS = (
    ("0-39",   0,  39,  "ضعيف"),
    ("40-69",  40, 69,  "متوسط"),
    ("70-100", 70, 100, "جيّد"),
)

BRAND_COLORS = {
    "brand": "#128C7E",
    "green": "#25D366",
    "ink":   "#0f172a",
}


# --------------------------------------------------------------------------- #
# مساعدات داخلية                                                               #
# --------------------------------------------------------------------------- #
def _status_of(p: dict) -> str:
    """رمز حالة قانونيّ (يقبل العربيّ والإنجليزيّ). فارغ/غير معروف → ''."""
    s = p.get("status")
    if s is None:
        return ""
    raw = str(s).strip()
    if raw in _STATUS_CANON:
        return _STATUS_CANON[raw]
    return _STATUS_CANON.get(raw.lower(), raw.lower())


def _to_int_score(value) -> int | None:
    """حوّل درجة التدقيق إلى عدد صحيح ضمن 0..100، أو None إن تعذّر."""
    if value is None or value == "":
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if n < 0:
        n = 0
    if n > 100:
        n = 100
    return n


def _has_website(p: dict) -> bool:
    return bool(str(p.get("website") or "").strip())


def _pct(part: int, whole: int) -> float:
    """نسبة مئوية مقرّبة لمنزلة واحدة (0.0 إن كان المقام صفراً)."""
    if not whole:
        return 0.0
    return round(part * 100.0 / whole, 1)


def _band_for(score: int) -> str:
    for key, lo, hi, _label in _SCORE_BANDS:
        if lo <= score <= hi:
            return key
    return _SCORE_BANDS[-1][0]


def _sorted_counts(counter: dict) -> list[tuple[str, int]]:
    """رتّب التوزيع تنازلياً بالعدد ثم أبجدياً بالمفتاح (نتيجة مستقرّة)."""
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


# --------------------------------------------------------------------------- #
# حساب الإحصاءات                                                               #
# --------------------------------------------------------------------------- #
def compute_stats(prospects: list[dict]) -> dict:
    """احسب إحصاءات أداء الحملة/الـCRM من قائمة الفرص.

    تُعيد قاموساً يحوي:
      total, by_status, sent, replied, interested, customers,
      conversion_rate (عملاء/أُرسل ٪)، reply_rate (ردّ/أُرسل ٪)،
      by_category, by_city، by_audit_band (مع تسميات الشرائح)،
      no_website، generated_at.
    """
    prospects = list(prospects or [])
    total = len(prospects)

    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_city: dict[str, int] = {}
    by_audit_band: dict[str, int] = {key: 0 for key, *_ in _SCORE_BANDS}

    sent = replied = interested = customers = no_website = scored = 0
    opened = clicked = targets = 0

    def _num(p, *keys):
        for k in keys:
            v = p.get(k)
            if v not in (None, ""):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
        return 0

    for p in prospects:
        st = _status_of(p)
        by_status[st] = by_status.get(st, 0) + 1

        if st in _SENT_STATUSES:
            sent += 1
        if st in _REPLIED_STATUSES:
            replied += 1
        if st in _INTERESTED_STATUSES:
            interested += 1
        if st in _CUSTOMER_STATUSES:
            customers += 1

        # تواصُل فعليّ (حتى لو بقيت الحالة «أُرسل» بلا ترقية): وجود آخر تواصل.
        if not (st in _SENT_STATUSES) and str(p.get("last_contacted_at") or "").strip():
            sent += 1  # أُرسل له فعلاً وإن لم تُحدَّث حالته
        if _num(p, "opens") > 0 or _num(p, "email_opens") > 0:
            opened += 1
        if _num(p, "clicks") > 0:
            clicked += 1
        if _num(p, "is_target") > 0:
            targets += 1

        cat = str(p.get("category") or "").strip() or "غير مصنّف"
        by_category[cat] = by_category.get(cat, 0) + 1

        city = str(p.get("city") or "").strip() or "غير محددة"
        by_city[city] = by_city.get(city, 0) + 1

        score = _to_int_score(p.get("audit_score"))
        if score is not None:
            by_audit_band[_band_for(score)] += 1
            scored += 1

        if not _has_website(p):
            no_website += 1

    # عميل واحد على الأقل يُحتسب ضمن «أُرسل»؛ لكن إن وردت بيانات غريبة نحمي القسمة.
    conversion_rate = _pct(customers, sent)
    reply_rate = _pct(replied, sent)
    open_rate = _pct(opened, sent)
    click_rate = _pct(clicked, sent)

    # قمع التحويل (المراحل بالترتيب مع نسبة كل مرحلة من الإجمالي).
    funnel = [
        {"key": "total",    "label": "الإجمالي",  "count": total},
        {"key": "targets",  "label": "مُستهدَف",   "count": targets},
        {"key": "sent",     "label": "أُرسل",      "count": sent},
        {"key": "opened",   "label": "فُتح",       "count": opened},
        {"key": "clicked",  "label": "نُقر الرابط", "count": clicked},
        {"key": "replied",  "label": "ردّ/مهتم",   "count": replied},
        {"key": "customer", "label": "عميل",       "count": customers},
    ]
    base = total or 1
    for f in funnel:
        f["pct"] = round(f["count"] * 100.0 / base, 1)

    audit_bands = [
        {
            "key": key,
            "label": label,
            "range": key,
            "count": by_audit_band[key],
            "pct": _pct(by_audit_band[key], scored),
        }
        for key, _lo, _hi, label in _SCORE_BANDS
    ]

    return {
        "total": total,
        "sent": sent,
        "replied": replied,
        "interested": interested,
        "customers": customers,
        "opened": opened,
        "clicked": clicked,
        "targets": targets,
        "no_website": no_website,
        "scored": scored,
        "conversion_rate": conversion_rate,
        "reply_rate": reply_rate,
        "open_rate": open_rate,
        "click_rate": click_rate,
        "funnel": funnel,
        "by_status": by_status,
        "by_category": by_category,
        "by_city": by_city,
        "by_audit_band": audit_bands,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# --------------------------------------------------------------------------- #
# عرض اللوحة (HTML)                                                            #
# --------------------------------------------------------------------------- #
def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _kpi_card(value, label, accent="brand") -> str:
    color = BRAND_COLORS.get(accent, BRAND_COLORS["ink"])
    return (
        '<div class="kpi">'
        f'<div class="kpi-v" style="color:{color}">{_e(value)}</div>'
        f'<div class="kpi-l">{_e(label)}</div>'
        '</div>'
    )


def _funnel_html(funnel: list[dict]) -> str:
    """يبني قمعاً بصريًّا أفقيًّا: كل مرحلة بطاقة فيها العدد والنسبة وشريط متناقص."""
    if not funnel:
        return '<p class="muted">لا توجد بيانات.</p>'
    cells = []
    for f in funnel:
        cells.append(
            '<div class="fn-step">'
            f'<div class="fn-bar" style="height:{max(6, min(100, f.get("pct", 0)))}%"></div>'
            f'<div class="fn-num">{_e(f.get("count", 0))}</div>'
            f'<div class="fn-lbl">{_e(f.get("label", ""))}</div>'
            f'<div class="fn-pct">{_e(f.get("pct", 0))}%</div>'
            '</div>'
        )
    return '<div class="funnel">' + "".join(cells) + '</div>'


def _bar_rows(items: list[tuple[str, int]], total: int,
              label_map: dict | None = None, color="brand") -> str:
    """ولّد صفوف الأعمدة الأفقية (عرض النسبة بالـCSS)."""
    if not items:
        return '<p class="muted">لا توجد بيانات.</p>'
    max_v = max(v for _k, v in items) or 1
    bar_color = BRAND_COLORS.get(color, BRAND_COLORS["brand"])
    rows = []
    for key, count in items:
        label = (label_map or {}).get(key, key) if label_map else key
        if not label:
            label = STATUS_LABELS.get("", "غير محدد")
        width = max(2, round(count * 100.0 / max_v))
        share = _pct(count, total)
        rows.append(
            '<div class="bar-row">'
            f'<div class="bar-label" title="{_e(label)}">{_e(label)}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill" style="width:{width}%;background:{bar_color}"></div>'
            '</div>'
            f'<div class="bar-num">{_e(count)} <span class="muted">({share}%)</span></div>'
            '</div>'
        )
    return "".join(rows)


_DASH_CSS = """
*{box-sizing:border-box;margin:0;padding:0;font-family:"Segoe UI",Tahoma,"Cairo",sans-serif}
body{background:#eef1f4;color:var(--ink);line-height:1.6}
.wrap{max-width:980px;margin:0 auto;padding:24px}
header{display:flex;align-items:center;gap:12px;margin-bottom:8px}
.logo{width:42px;height:42px;border-radius:12px;background:var(--brand);color:#fff;
  display:flex;align-items:center;justify-content:center;font-weight:800;font-size:20px}
h1{font-size:24px;color:var(--ink)}
.sub{color:#7b8794;font-size:13px;margin-bottom:20px}
.card{background:#fff;border:1px solid #e3e8ee;border-radius:14px;padding:20px;margin-bottom:16px}
h2{font-size:17px;margin-bottom:14px;color:var(--ink)}
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:16px}
.kpi{background:#fff;border:1px solid #e3e8ee;border-radius:14px;padding:16px;text-align:center}
.kpi-v{font-size:26px;font-weight:800;line-height:1.2}
.kpi-l{font-size:12px;color:#7b8794;margin-top:4px}
.muted{color:#7b8794;font-size:13px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.bar-row{display:grid;grid-template-columns:120px 1fr 92px;align-items:center;gap:10px;margin:8px 0}
.bar-label{font-size:13px;font-weight:600;color:#5b6b7b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-track{background:#eef1f4;border-radius:8px;height:14px;overflow:hidden}
.bar-fill{height:100%;border-radius:8px;min-width:4px;transition:width .2s}
.bar-num{font-size:13px;text-align:left;font-variant-numeric:tabular-nums}
footer{text-align:center;color:#9aa5b1;font-size:12px;margin-top:18px}
.funnel{display:flex;gap:8px;align-items:flex-end;min-height:150px}
.fn-step{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;
  background:#f6f8fb;border:1px solid #e3e8ee;border-radius:12px;padding:8px 4px;position:relative;min-height:140px}
.fn-bar{width:60%;background:linear-gradient(180deg,var(--brand),var(--green));border-radius:8px 8px 4px 4px;min-height:6px;margin-bottom:auto}
.fn-num{font-size:20px;font-weight:800;color:var(--ink);margin-top:6px}
.fn-lbl{font-size:12px;color:#5b6b7b;text-align:center}
.fn-pct{font-size:11px;color:#9aa5b1}
@media(max-width:760px){.kpis{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}
  .bar-row{grid-template-columns:90px 1fr 80px}
  .funnel{flex-wrap:wrap}.fn-step{min-width:80px}}
"""


def render_dashboard(stats: dict, brand: str = "واجهة") -> str:
    """ابنِ صفحة لوحة HTML عربية RTL مسطّحة من ناتج compute_stats."""
    stats = stats or {}
    total = stats.get("total", 0)

    kpis = "".join([
        _kpi_card(stats.get("total", 0), "إجمالي الفرص", "ink"),
        _kpi_card(stats.get("sent", 0), "أُرسل", "brand"),
        _kpi_card(stats.get("replied", 0), "ردّ", "brand"),
        _kpi_card(stats.get("interested", 0), "مهتم", "green"),
        _kpi_card(stats.get("customers", 0), "عملاء", "green"),
        _kpi_card(f"{stats.get('conversion_rate', 0)}%", "معدل التحويل", "green"),
    ])

    funnel_html = _funnel_html(stats.get("funnel", []))

    status_items = _sorted_counts(stats.get("by_status", {}))
    status_bars = _bar_rows(status_items, total, STATUS_LABELS, "brand")

    cat_items = _sorted_counts(stats.get("by_category", {}))[:10]
    cat_bars = _bar_rows(cat_items, total, None, "green")

    city_items = _sorted_counts(stats.get("by_city", {}))[:10]
    city_bars = _bar_rows(city_items, total, None, "brand")

    audit_bands = stats.get("by_audit_band", [])
    band_items = [(b["range"], b["count"]) for b in audit_bands]
    band_labels = {b["range"]: f'{b["label"]} ({b["range"]})' for b in audit_bands}
    band_bars = _bar_rows(band_items, stats.get("scored", 0) or total, band_labels, "green")

    reply_rate = stats.get("reply_rate", 0)
    no_website = stats.get("no_website", 0)
    generated_at = stats.get("generated_at", "")

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>لوحة أداء الحملة — {_e(brand)}</title>
<style>
:root{{--brand:{BRAND_COLORS['brand']};--green:{BRAND_COLORS['green']};--ink:{BRAND_COLORS['ink']}}}
{_DASH_CSS}
</style></head>
<body><div class="wrap">

<header>
  <div class="logo">و</div>
  <div>
    <h1>لوحة أداء الحملة</h1>
    <div class="sub">{_e(brand)} · إجمالي {_e(total)} فرصة · حُدّثت {_e(generated_at)}</div>
  </div>
</header>

<div class="kpis">{kpis}</div>

<div class="card">
  <h2>قمع التحويل</h2>
  {funnel_html}
</div>

<div class="card">
  <h2>المؤشّرات الإضافية</h2>
  <div class="grid2">
    <div class="bar-row">
      <div class="bar-label">نسبة الفتح</div>
      <div class="bar-track"><div class="bar-fill"
        style="width:{min(100, stats.get('open_rate', 0))}%;background:var(--brand)"></div></div>
      <div class="bar-num">{_e(stats.get('open_rate', 0))}%</div>
    </div>
    <div class="bar-row">
      <div class="bar-label">نسبة النقر</div>
      <div class="bar-track"><div class="bar-fill"
        style="width:{min(100, stats.get('click_rate', 0))}%;background:var(--green)"></div></div>
      <div class="bar-num">{_e(stats.get('click_rate', 0))}%</div>
    </div>
    <div class="bar-row">
      <div class="bar-label">نسبة الردّ</div>
      <div class="bar-track"><div class="bar-fill"
        style="width:{min(100, reply_rate)}%;background:var(--brand)"></div></div>
      <div class="bar-num">{_e(reply_rate)}%</div>
    </div>
    <div class="bar-row">
      <div class="bar-label">بلا موقع</div>
      <div class="bar-track"><div class="bar-fill"
        style="width:{min(100, _pct(no_website, total))}%;background:var(--green)"></div></div>
      <div class="bar-num">{_e(no_website)}</div>
    </div>
  </div>
</div>

<div class="grid2">
  <div class="card">
    <h2>التوزيع حسب الحالة</h2>
    {status_bars}
  </div>
  <div class="card">
    <h2>التوزيع حسب الفئة</h2>
    {cat_bars}
  </div>
</div>

<div class="grid2">
  <div class="card">
    <h2>التوزيع حسب المدينة</h2>
    {city_bars}
  </div>
  <div class="card">
    <h2>شرائح درجة تدقيق الموقع</h2>
    {band_bars}
  </div>
</div>

<footer>هوية «{_e(brand)}» · تقرير داخلي — لا يُفهرس</footer>
</div></body></html>"""


# --------------------------------------------------------------------------- #
# اختبار ذاتي                                                                  #
# --------------------------------------------------------------------------- #
def _demo_prospects() -> list[dict]:
    """~12 فرصة وهمية بحالات/فئات/مدن/درجات متنوّعة لإثبات العقد."""
    return [
        {"name": "مطعم الذواقة", "category": "مطعم", "city": "الرياض",
         "status": "customer", "audit_score": 22, "website": ""},
        {"name": "كافيه الركن", "category": "مقهى", "city": "الرياض",
         "status": "interested", "audit_score": 35, "website": ""},
        {"name": "عيادة النور", "category": "عيادة أسنان", "city": "جدة",
         "status": "replied", "audit_score": 58, "website": "https://noor.sa"},
        {"name": "صالون الأناقة", "category": "صالون", "city": "جدة",
         "status": "sent", "audit_score": 41, "website": ""},
        {"name": "فندق الواحة", "category": "فندق", "city": "مكة",
         "status": "customer", "audit_score": 77, "website": "https://oasis.sa"},
        {"name": "مطعم البحر", "category": "مطعم", "city": "الدمام",
         "status": "sent", "audit_score": 30, "website": ""},
        {"name": "جيم القوة", "category": "نادي رياضي", "city": "الرياض",
         "status": "lost", "audit_score": 49, "website": ""},
        {"name": "بيطري الرحمة", "category": "بيطري", "city": "الرياض",
         "status": "new", "audit_score": None, "website": ""},
        {"name": "متجر الهدايا", "category": "متجر", "city": "جدة",
         "status": "queued", "audit_score": 15, "website": ""},
        {"name": "سبا الاسترخاء", "category": "سبا", "city": "مكة",
         "status": "negotiating", "audit_score": 68, "website": "https://spa.sa"},
        {"name": "عيادة الشفاء", "category": "عيادة", "city": "الدمام",
         "status": "replied", "audit_score": 84, "website": "https://shifa.sa"},
        {"name": "كوفي تايم", "category": "مقهى", "city": "الرياض",
         "status": "", "audit_score": "72", "website": ""},
    ]


def _self_test() -> None:
    prospects = _demo_prospects()

    # 1) compute_stats يرجّع العقد المتوقّع.
    stats = compute_stats(prospects)
    assert stats["total"] == 12, stats["total"]
    assert sum(stats["by_status"].values()) == 12
    assert stats["customers"] == 2, stats["customers"]
    assert stats["sent"] >= stats["customers"]
    assert stats["replied"] >= stats["customers"]
    # شرائح التدقيق: ثلاث شرائح، والمجموع = عدد المُقيَّمين.
    bands = {b["range"]: b["count"] for b in stats["by_audit_band"]}
    assert set(bands) == {"0-39", "40-69", "70-100"}, bands
    assert sum(bands.values()) == stats["scored"], (bands, stats["scored"])
    # من بلا موقع: العدّ صحيح.
    expected_no_site = sum(1 for p in prospects if not str(p.get("website") or "").strip())
    assert stats["no_website"] == expected_no_site, stats["no_website"]
    # معدّل التحويل = عملاء/أُرسل ٪.
    assert stats["conversion_rate"] == round(stats["customers"] * 100.0 / stats["sent"], 1)
    # توزيع المدن/الفئات غير فارغ.
    assert stats["by_city"] and stats["by_category"]

    # 2) compute_stats لا ينهار على المدخلات الفارغة/الناقصة.
    empty = compute_stats([])
    assert empty["total"] == 0 and empty["conversion_rate"] == 0.0
    partial = compute_stats([{"name": "x"}, {"status": "sent"}])
    assert partial["total"] == 2

    # 3) render_dashboard ينتج صفحة غير فارغة تحوي «معدل التحويل».
    page = render_dashboard(stats)
    assert isinstance(page, str) and len(page) > 1000
    assert "معدل التحويل" in page
    assert 'dir="rtl"' in page
    assert "noindex" in page
    assert BRAND_COLORS["brand"] in page
    # يحوي تسمية حالة عربية واحدة على الأقل.
    assert "عميل" in page

    # طباعة موجز للتحقّق اليدوي.
    print("=== إحصاءات الحملة (اختبار ذاتي) ===")
    print(f"الإجمالي         : {stats['total']}")
    print(f"أُرسل            : {stats['sent']}")
    print(f"ردّ              : {stats['replied']}")
    print(f"مهتم             : {stats['interested']}")
    print(f"عملاء            : {stats['customers']}")
    print(f"معدل التحويل     : {stats['conversion_rate']}%")
    print(f"نسبة الردّ       : {stats['reply_rate']}%")
    print(f"بلا موقع         : {stats['no_website']}")
    print("التوزيع حسب الحالة:")
    for k, v in _sorted_counts(stats["by_status"]):
        print(f"   {STATUS_LABELS.get(k, k) or 'غير محدد':<10} : {v}")
    print("شرائح درجة التدقيق:")
    for b in stats["by_audit_band"]:
        print(f"   {b['label']} ({b['range']:<7}): {b['count']}")
    print(f"\nطول صفحة اللوحة  : {len(page)} حرف")
    print("نجح الاختبار الذاتي ✓")


if __name__ == "__main__":
    _self_test()
