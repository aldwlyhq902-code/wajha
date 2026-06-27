"""
محرّك تدقيق موقع إلكتروني (Website Audit Engine) — stdlib فقط.
==============================================================================
يجلب صفحة موقع، يحلّل HTML، ويُعيد تقييماً واقعياً على عدّة محاور مع تركيز
على «الحجز/التحويل» (الأعلى وزناً)، ثم يولّد تقرير HTML عربياً RTL مستقلاً
يعرض الدرجة ونقاط القوة والمشاكل وخطة تطوير وعرضَي خدمة وزر واتساب.

العقد العام (للاستيراد):
    audit_site(url, timeout=15) -> dict
    render_report(business: dict, audit: dict, brand="واجهة") -> str

مثال:
    from audit import audit_site, render_report
    a = audit_site("https://example.com")
    html_page = render_report({"name": "مطعم الذوق", "phone": "0501234567"}, a)

الهوية «واجهة»:  --brand:#128C7E ; --green:#25D366 ; --ink:#0f172a
"""

from __future__ import annotations

import html
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import quote, urlparse

# إعادة استخدام أدوات الهاتف من محرّك العملاء (متوافقة مع بقية النظام)
try:
    from leads import normalize_phone
except Exception:  # احتياط: لو استُورد الملف منفرداً بلا حزمة leads
    def normalize_phone(raw, default_cc: str = "966"):  # type: ignore
        if not raw:
            return None
        has_plus = str(raw).strip().startswith("+")
        digits = re.sub(r"\D", "", str(raw))
        if not digits:
            return None
        if has_plus:
            return digits
        if digits.startswith("00"):
            return digits[2:]
        if digits.startswith("0"):
            return default_cc + digits[1:]
        if digits.startswith(default_cc):
            return digits
        return default_cc + digits

# إخراج UTF-8 آمن على ويندوز
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# أوزان المحاور (المجموع = 100). الأعلى للحجز/التحويل ثم التواصل.              #
# --------------------------------------------------------------------------- #
WEIGHTS = {
    "booking":  26,   # الحجز/التحويل
    "contact":  20,   # التواصل
    "mobile":   14,   # توافق الجوال
    "security": 12,   # الأمان (HTTPS)
    "seo":      12,   # تهيئة محركات البحث
    "content":  10,   # المحتوى (خدمات/أسعار/ساعات)
    "weight":    6,   # الحداثة/الوزن
}
assert sum(WEIGHTS.values()) == 100, "أوزان المحاور يجب أن تساوي 100"

DIM_LABELS = {
    "booking":  "الحجز/التحويل",
    "contact":  "التواصل",
    "mobile":   "توافق الجوال",
    "security": "الأمان (HTTPS)",
    "seo":      "تهيئة محركات البحث (SEO)",
    "content":  "المحتوى",
    "weight":   "الحداثة والوزن",
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# كلمات دالّة (عربي + إنجليزي)
_BOOKING_WORDS = (
    "احجز", "احجزي", "حجز", "موعد", "مواعيد", "احجز الآن", "اطلب الآن", "اطلب",
    "booking", "book now", "reserve", "reservation", "appointment", "schedule",
    "order now", "اطلب موعد", "حجز موعد",
)
_SERVICE_WORDS = ("خدمات", "خدماتنا", "services", "our services", "ماذا نقدم")
_PRICE_WORDS = ("سعر", "أسعار", "الأسعار", "ريال", "ر.س", "sar", "price", "pricing", "تكلفة", "باقات")
_HOURS_WORDS = ("ساعات العمل", "أوقات العمل", "مواعيد العمل", "opening hours",
                "working hours", "hours", "نفتح", "مفتوح")


# --------------------------------------------------------------------------- #
# جلب الصفحة                                                                   #
# --------------------------------------------------------------------------- #
def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def _fetch(url: str, timeout: int = 15) -> tuple[bool, str, str, str]:
    """
    يُعيد (ok, final_url, html_text, error).
    يتبع redirect (urllib تلقائياً)، ويتعامل مع SSL/مهلة/أخطاء بأمان.
    """
    url = _normalize_url(url)
    if not url:
        return False, url, "", "رابط فارغ"

    ctx = ssl.create_default_context()
    # نتساهل في التحقق من الشهادة حتى لا نفشل الجلب لأسباب شهادة فقط
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ar,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            final_url = resp.geturl() or url
            raw = resp.read(2_500_000)  # حد أقصى ~2.5MB
            charset = "utf-8"
            ctype = resp.headers.get("Content-Type", "") or ""
            m = re.search(r"charset=([\w\-]+)", ctype, re.I)
            if m:
                charset = m.group(1)
            text = raw.decode(charset, errors="replace")
            # كشف charset من <meta> لو لزم
            if charset.lower() == "utf-8":
                mm = re.search(r'charset=["\']?\s*([\w\-]+)', text[:2000], re.I)
                if mm and mm.group(1).lower() not in ("utf-8", "utf8"):
                    try:
                        text = raw.decode(mm.group(1), errors="replace")
                    except Exception:
                        pass
            return True, final_url, text, ""
    except urllib.error.HTTPError as e:
        return False, url, "", f"خطأ HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, url, "", f"تعذّر الوصول: {getattr(e, 'reason', e)}"
    except (TimeoutError, ssl.SSLError) as e:
        return False, url, "", f"مهلة/أمان: {e}"
    except Exception as e:
        return False, url, "", f"خطأ غير متوقّع: {e}"


# --------------------------------------------------------------------------- #
# أدوات تحليل HTML خفيفة                                                       #
# --------------------------------------------------------------------------- #
def _strip_tags(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _has_any(haystack: str, needles) -> bool:
    return any(n in haystack for n in needles)


def _meta_content(html_text: str, *, name=None, prop=None) -> str:
    """يستخرج محتوى وسم meta حسب name= أو property=."""
    for m in re.finditer(r"(?is)<meta\b[^>]*>", html_text):
        tag = m.group(0)
        if name:
            k = re.search(r'name\s*=\s*["\']?\s*([^"\'\s>]+)', tag, re.I)
            if not (k and k.group(1).lower() == name.lower()):
                continue
        if prop:
            k = re.search(r'property\s*=\s*["\']?\s*([^"\'\s>]+)', tag, re.I)
            if not (k and k.group(1).lower() == prop.lower()):
                continue
        c = re.search(r'content\s*=\s*"([^"]*)"', tag, re.I) or \
            re.search(r"content\s*=\s*'([^']*)'", tag, re.I)
        if c:
            return html.unescape(c.group(1)).strip()
    return ""


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


# --------------------------------------------------------------------------- #
# المحرّك الرئيسي                                                              #
# --------------------------------------------------------------------------- #
def audit_site(url: str, timeout: int = 15) -> dict:
    """
    يُدقّق موقعاً ويُعيد القاموس الموصوف في العقد.
    عند فشل الجلب: ok=False و score=0 ودرجات صفرية ومشكلة واضحة.
    """
    url_in = _normalize_url(url)
    ok, final_url, page, err = _fetch(url_in, timeout=timeout)

    result = {
        "url": final_url or url_in,
        "ok": ok,
        "score": 0,
        "dims": {k: 0 for k in WEIGHTS},
        "issues": [],
        "strengths": [],
        "has_booking": False,
        "has_whatsapp": False,
        "https": False,
        "mobile": False,
        "title": "",
        "desc": "",
    }

    if not ok:
        result["issues"] = [
            f"الموقع لا يفتح أو لا يستجيب ({err}). هذا أكبر عائق: زائر لا يصل = عميل ضائع.",
            "ننصح بفحص الاستضافة/الدومين أو إعادة بناء موقع سريع وموثوق.",
        ]
        return result

    low = page.lower()
    text = _strip_tags(page)
    text_low = text.lower()
    blob = (low + " " + text_low)  # للبحث في الوسوم والنص معاً

    https = (urlparse(result["url"]).scheme == "https")
    result["https"] = https

    # عنوان ووصف
    mt = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    title = html.unescape(_strip_tags(mt.group(1))) if mt else ""
    desc = _meta_content(page, name="description") or _meta_content(page, prop="og:description")
    og_title = _meta_content(page, prop="og:title")
    result["title"] = title[:200]
    result["desc"] = (desc or "")[:300]

    # عناصر مشتركة
    has_form = bool(re.search(r"(?is)<form\b", page))
    has_viewport = bool(re.search(
        r'(?is)<meta[^>]+name\s*=\s*["\']?viewport', page))
    viewport_scales = bool(re.search(
        r'(?is)<meta[^>]+name\s*=\s*["\']?viewport[^>]*content\s*=\s*["\'][^"\']*width\s*=\s*device-width',
        page))
    n_imgs = len(re.findall(r"(?is)<img\b", page))
    n_responsive_hints = len(re.findall(r"@media", page)) + \
        len(re.findall(r'class\s*=\s*["\'][^"\']*(?:col-|row|flex|grid|container)', page, re.I))
    has_wa = bool(re.search(r"(?:wa\.me/|api\.whatsapp\.com|whatsapp://|web\.whatsapp\.com)", low))
    has_tel = bool(re.search(r'href\s*=\s*["\']tel:', low))
    has_mail = bool(re.search(r'href\s*=\s*["\']mailto:', low))
    has_map = bool(re.search(r"(?:google\.com/maps|maps\.app\.goo\.gl|goo\.gl/maps|<iframe[^>]+maps)", low))
    page_kb = len(page.encode("utf-8")) / 1024.0

    has_booking = _has_any(blob, [w.lower() for w in _BOOKING_WORDS])
    result["has_booking"] = has_booking
    result["has_whatsapp"] = has_wa
    result["mobile"] = has_viewport

    issues: list[str] = []
    strengths: list[str] = []
    dims: dict[str, float] = {}

    # --- 1) الحجز/التحويل (الأهم) ---------------------------------------- #
    b = 0.0
    if has_booking:
        b += 55
        strengths.append("الموقع يستخدم لغة تحفيز على الإجراء (احجز/اطلب/موعد).")
    else:
        issues.append("لا توجد دعوة واضحة للحجز أو الطلب — الزائر لا يعرف الخطوة التالية، "
                      "وهذا يُهدر معظم الزيارات.")
    if has_form:
        b += 25
    else:
        issues.append("لا يوجد نموذج حجز/تواصل مباشر داخل الموقع، فالعميل مضطر للبحث عن طريقة للتواصل.")
    if has_wa:
        b += 20  # واتساب قناة تحويل قوية محلياً
    dims["booking"] = _clamp(b)

    # --- 2) التواصل ------------------------------------------------------ #
    c = 0.0
    contact_bits = []
    if has_wa:
        c += 35; contact_bits.append("واتساب")
    if has_tel:
        c += 25; contact_bits.append("اتصال مباشر")
    if has_map:
        c += 20; contact_bits.append("خريطة موقع")
    if has_form:
        c += 12; contact_bits.append("نموذج")
    if has_mail:
        c += 8; contact_bits.append("بريد")
    if contact_bits:
        strengths.append("قنوات تواصل متاحة: " + "، ".join(contact_bits) + ".")
    if not has_wa:
        issues.append("لا يوجد زر واتساب — وهو أسرع قناة تحويل للعملاء محلياً.")
    if not (has_tel or has_map):
        issues.append("يصعب الوصول لرقم الهاتف أو موقع المنشأة على الخريطة بسهولة.")
    dims["contact"] = _clamp(c)

    # --- 3) توافق الجوال ------------------------------------------------- #
    m = 0.0
    if has_viewport:
        m += 55
        if viewport_scales:
            m += 20
    else:
        issues.append("الموقع غير مهيّأ للجوال (لا يوجد viewport)، ومعظم الزيارات من الجوال.")
    if n_responsive_hints >= 2:
        m += 25
        strengths.append("التصميم يبدو متجاوباً مع أحجام الشاشات.")
    elif n_responsive_hints == 0 and has_viewport:
        m -= 5
    dims["mobile"] = _clamp(m)

    # --- 4) الأمان ------------------------------------------------------- #
    s = 100.0 if https else 0.0
    if https:
        strengths.append("الاتصال آمن ومشفّر (HTTPS).")
    else:
        issues.append("الموقع بلا HTTPS — المتصفّحات تُحذّر الزوّار ويتراجع ترتيبه في البحث.")
    dims["security"] = s

    # --- 5) SEO ---------------------------------------------------------- #
    se = 0.0
    if title:
        se += 30
        if 10 <= len(title) <= 65:
            se += 8
    else:
        issues.append("لا يوجد عنوان صفحة (title) واضح — يضعف الظهور في نتائج البحث.")
    if desc:
        se += 30
        if 50 <= len(desc) <= 165:
            se += 7
    else:
        issues.append("لا يوجد وصف ميتا (meta description) — جوجل يعرض نصاً عشوائياً بدلاً منه.")
    if og_title or _meta_content(page, prop="og:image"):
        se += 25
        strengths.append("يحتوي وسوم مشاركة (Open Graph) لعرض أنيق على المنصات.")
    dims["seo"] = _clamp(se)

    # --- 6) المحتوى ------------------------------------------------------ #
    ct = 0.0
    found_content = []
    if _has_any(blob, _SERVICE_WORDS):
        ct += 35; found_content.append("خدمات")
    if _has_any(blob, _PRICE_WORDS):
        ct += 30; found_content.append("أسعار")
    if _has_any(blob, _HOURS_WORDS):
        ct += 25; found_content.append("ساعات عمل")
    if len(text) >= 600:
        ct += 10
    if found_content:
        strengths.append("محتوى مفيد متوفّر: " + "، ".join(found_content) + ".")
    else:
        issues.append("المحتوى شحيح — لا تظهر الخدمات أو الأسعار أو ساعات العمل بوضوح.")
    if "أسعار" not in "".join(found_content) and "ساعات عمل" not in "".join(found_content):
        issues.append("لا توجد أسعار ولا ساعات عمل ظاهرة، فالعميل يتردّد قبل أن يقرّر.")
    dims["content"] = _clamp(ct)

    # --- 7) الحداثة/الوزن ------------------------------------------------ #
    w = 100.0
    if page_kb > 1500:
        w -= 45
        issues.append("صفحة ثقيلة جداً (الحجم كبير) — تبطئ التحميل خصوصاً على بيانات الجوال.")
    elif page_kb > 700:
        w -= 20
    if n_imgs > 60:
        w -= 25
        issues.append("عدد الصور كبير جداً دون تحسين على الأرجح، ما يزيد زمن التحميل.")
    elif n_imgs > 30:
        w -= 10
    if "<table" in low and n_responsive_hints == 0:
        w -= 15  # تخطيط قديم بالجداول
    if page_kb <= 700 and w >= 90:
        strengths.append("الصفحة خفيفة وسريعة التحميل نسبياً.")
    dims["weight"] = _clamp(w)

    # --- الدرجة الإجمالية الموزونة -------------------------------------- #
    total = 0.0
    for k, wt in WEIGHTS.items():
        total += dims.get(k, 0.0) * wt / 100.0
    score = int(round(_clamp(total)))

    # تقليم وتنظيف، مع إبقاء أهم نقاط القوة والمشاكل
    result["dims"] = {k: int(round(dims.get(k, 0.0))) for k in WEIGHTS}
    result["score"] = score
    # أزل التكرار مع الحفاظ على الترتيب
    result["issues"] = list(dict.fromkeys(issues))
    result["strengths"] = list(dict.fromkeys(strengths))
    return result


# --------------------------------------------------------------------------- #
# مولّد التقرير HTML                                                           #
# --------------------------------------------------------------------------- #
def _score_color(score: int) -> str:
    if score >= 75:
        return "#16a34a"
    if score >= 50:
        return "#d97706"
    return "#dc2626"


def _score_label(score: int) -> str:
    if score >= 80:
        return "ممتاز"
    if score >= 65:
        return "جيد"
    if score >= 45:
        return "متوسّط — قابل للتحسين"
    return "ضعيف — يحتاج عملاً عاجلاً"


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _phased_plan(audit: dict) -> list[tuple[str, str, list[str]]]:
    """خطة تطوير ٣ مراحل مبنية على المشاكل الفعلية."""
    dims = audit.get("dims", {})
    p1, p2, p3 = [], [], []

    if not audit.get("https"):
        p1.append("تفعيل شهادة HTTPS وتأمين الموقع بالكامل.")
    if not audit.get("mobile"):
        p1.append("جعل الموقع متجاوباً تماماً مع الجوال.")
    if dims.get("seo", 0) < 60:
        p1.append("ضبط عنوان ووصف الصفحة ووسوم المشاركة للظهور في البحث.")
    if not p1:
        p1.append("تحسينات سريعة على السرعة وتنظيف الصفحة الرئيسية.")

    if not audit.get("has_whatsapp"):
        p2.append("إضافة زر واتساب عائم ورقم اتصال مباشر.")
    if not audit.get("has_booking") or dims.get("booking", 0) < 60:
        p2.append("إضافة دعوة واضحة للحجز/الطلب في أعلى الصفحة.")
    if dims.get("content", 0) < 60:
        p2.append("صفحة خدمات وأسعار وساعات عمل واضحة.")
    if not p2:
        p2.append("تقوية صفحات الخدمات والمحتوى التسويقي.")

    p3.append("ربط نظام حجز/مواعيد ذاتي يعمل ٢٤ ساعة بلا تدخّل.")
    p3.append("مساعد واتساب آلي للرد على الاستفسارات وتأكيد الحجوزات.")
    p3.append("متابعة الأداء وتحسين معدّل التحويل شهرياً.")
    return [
        ("المرحلة الأولى — الأساسات", "أسبوع 1", p1[:4]),
        ("المرحلة الثانية — التحويل", "أسبوع 2", p2[:4]),
        ("المرحلة الثالثة — الأتمتة والنمو", "مستمر", p3[:4]),
    ]


def render_report(business: dict, audit: dict, brand: str = "واجهة",
                  cta_url: str | None = None, pixel_url: str | None = None) -> str:
    """يولّد صفحة HTML عربية RTL مستقلّة (noindex) كتقرير تدقيق + عرض خدمتين.

    cta_url   : إن مُرّر، يُستخدم لزر «تواصل عبر واتساب» (مثلاً رابط /go لتتبّع النقر).
    pixel_url : إن مُرّر، يُحقَن بكسل تتبّع شفّاف 1×1 لرصد فتح التقرير.
    """
    business = business or {}
    audit = audit or {}
    name = business.get("name") or "منشأتك"
    site = audit.get("url") or business.get("website") or ""
    score = int(audit.get("score") or 0)
    color = _score_color(score)
    label = _score_label(score)
    dims = audit.get("dims", {}) or {}

    intl = normalize_phone(business.get("phone") or business.get("whatsapp"))
    wa_msg = (f"السلام عليكم، شاهدت تقرير موقع «{name}» من {brand} "
              f"(الدرجة {score}/100) وأرغب بتطويره وإضافة الحجز عبر واتساب.")
    wa_link = cta_url or (f"https://wa.me/{intl}?text={quote(wa_msg)}"
                          if intl else f"https://wa.me/?text={quote(wa_msg)}")
    pixel_tag = (f'<img src="{_e(pixel_url)}" width="1" height="1" alt="" '
                 f'style="position:absolute;left:-9999px">' if pixel_url else "")

    # نقاط القوة (٣) والمشاكل (حتى ٥)
    strengths = (audit.get("strengths") or [])[:3]
    if not strengths:
        strengths = ["الموقع موجود — وهذه نقطة انطلاق نبني عليها."]
    issues = (audit.get("issues") or [])[:5]
    if not issues:
        issues = ["لا توجد مشاكل حرجة واضحة، لكن يمكن رفع معدّل التحويل أكثر."]

    # أشرطة المحاور
    dim_rows = ""
    for k, wt in WEIGHTS.items():
        v = int(dims.get(k, 0))
        dc = _score_color(v)
        dim_rows += (
            f'<div class="dim">'
            f'<div class="dim-head"><span>{_e(DIM_LABELS[k])}</span>'
            f'<span class="dim-val" style="color:{dc}">{v}/100</span></div>'
            f'<div class="bar"><i style="width:{v}%;background:{dc}"></i></div>'
            f'</div>'
        )

    strengths_html = "".join(f"<li>{_e(s)}</li>" for s in strengths)
    issues_html = "".join(f"<li>{_e(s)}</li>" for s in issues)

    plan = _phased_plan(audit)
    plan_html = ""
    for ph_title, ph_when, ph_items in plan:
        items = "".join(f"<li>{_e(i)}</li>" for i in ph_items)
        plan_html += (
            f'<div class="phase">'
            f'<div class="phase-top"><h4>{_e(ph_title)}</h4>'
            f'<span class="when">{_e(ph_when)}</span></div>'
            f'<ul>{items}</ul></div>'
        )

    site_line = (f'<a href="{_e(site)}" target="_blank" rel="noopener">{_e(site)}</a>'
                 if site else "—")
    gen_dt = datetime.now().strftime("%Y-%m-%d %H:%M")

    # نسبة محيط الدائرة لشريط الدرجة الدائري
    circ = 326.7  # 2*pi*52
    dash = round(circ * score / 100.0, 1)

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>تقرير تدقيق موقع {_e(name)} — {_e(brand)}</title>
<style>
  :root{{ --brand:#128C7E; --green:#25D366; --ink:#0f172a; --muted:#64748b;
          --bg:#f1f5f9; --card:#ffffff; --line:#e2e8f0; }}
  *{{ box-sizing:border-box; }}
  body{{ margin:0; font-family:"Segoe UI",Tahoma,system-ui,sans-serif;
         background:var(--bg); color:var(--ink); line-height:1.7; }}
  .wrap{{ max-width:860px; margin:0 auto; padding:20px; }}
  .top{{ background:linear-gradient(135deg,var(--brand),#0d6e63);
         color:#fff; border-radius:18px; padding:26px; margin-bottom:18px;
         box-shadow:0 10px 30px rgba(18,140,126,.25); }}
  .top .brand{{ font-weight:800; letter-spacing:.5px; opacity:.95; }}
  .top h1{{ margin:6px 0 4px; font-size:26px; }}
  .top .site{{ font-size:14px; opacity:.9; word-break:break-all; }}
  .top .site a{{ color:#d1fae5; }}
  .card{{ background:var(--card); border:1px solid var(--line);
          border-radius:16px; padding:22px; margin-bottom:16px;
          box-shadow:0 4px 14px rgba(15,23,42,.04); }}
  h2{{ font-size:19px; margin:0 0 14px; color:var(--brand); }}
  h3{{ font-size:16px; margin:0 0 8px; }}
  .score-card{{ display:flex; align-items:center; gap:24px; flex-wrap:wrap; }}
  .ring{{ flex:0 0 auto; }}
  .ring .num{{ font-size:34px; font-weight:800; }}
  .score-meta{{ flex:1 1 240px; }}
  .score-meta .lbl{{ display:inline-block; padding:5px 14px; border-radius:999px;
        font-weight:700; color:#fff; font-size:14px; }}
  .dim{{ margin:12px 0; }}
  .dim-head{{ display:flex; justify-content:space-between; font-size:14px;
        font-weight:600; margin-bottom:5px; }}
  .dim-val{{ font-weight:800; }}
  .bar{{ background:#eef2f7; border-radius:999px; height:10px; overflow:hidden; }}
  .bar i{{ display:block; height:100%; border-radius:999px; transition:width .4s; }}
  ul{{ margin:6px 0; padding-inline-start:22px; }}
  li{{ margin:6px 0; }}
  .good li::marker{{ color:var(--green); }}
  .bad li::marker{{ color:#dc2626; }}
  .phase{{ border-inline-start:4px solid var(--brand); background:#f8fafc;
        border-radius:0 12px 12px 0; padding:12px 16px; margin:12px 0; }}
  .phase-top{{ display:flex; justify-content:space-between; align-items:center; }}
  .phase-top h4{{ margin:0; color:var(--ink); font-size:15px; }}
  .when{{ font-size:12px; color:var(--muted); background:#e2e8f0;
        padding:3px 10px; border-radius:999px; }}
  .offers{{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
  .offer{{ border:1px solid var(--line); border-radius:14px; padding:18px;
        background:linear-gradient(180deg,#fff,#f8fffd); }}
  .offer .pill{{ display:inline-block; background:var(--green); color:#063;
        font-weight:800; font-size:12px; padding:3px 10px; border-radius:999px;
        margin-bottom:8px; }}
  .offer h3{{ color:var(--brand); }}
  .offer p{{ color:var(--muted); font-size:14px; margin:6px 0 0; }}
  .cta{{ text-align:center; margin:24px 0 10px; }}
  .btn{{ display:inline-flex; align-items:center; gap:10px; background:var(--green);
        color:#053d2b; text-decoration:none; font-weight:800; font-size:18px;
        padding:16px 34px; border-radius:14px;
        box-shadow:0 8px 22px rgba(37,211,102,.4); }}
  .btn:hover{{ filter:brightness(1.05); }}
  .foot{{ text-align:center; color:var(--muted); font-size:12px; margin-top:18px; }}
  @media(max-width:560px){{ .offers{{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div class="wrap">

  <div class="top">
    <div class="brand">{_e(brand)} · تقرير تدقيق رقمي</div>
    <h1>{_e(name)}</h1>
    <div class="site">الموقع المفحوص: {site_line}</div>
  </div>

  <div class="card score-card">
    <div class="ring">
      <svg width="130" height="130" viewBox="0 0 130 130">
        <circle cx="65" cy="65" r="52" fill="none" stroke="#eef2f7" stroke-width="13"/>
        <circle cx="65" cy="65" r="52" fill="none" stroke="{color}" stroke-width="13"
          stroke-linecap="round" stroke-dasharray="{dash} {circ}"
          transform="rotate(-90 65 65)"/>
        <text x="65" y="60" text-anchor="middle" class="num" fill="{color}">{score}</text>
        <text x="65" y="82" text-anchor="middle" font-size="12" fill="#64748b">من 100</text>
      </svg>
    </div>
    <div class="score-meta">
      <h2 style="margin-bottom:8px">التقييم العام</h2>
      <span class="lbl" style="background:{color}">{_e(label)}</span>
      <p style="color:var(--muted);margin:12px 0 0">
        هذا التقييم يقيس جاهزية موقعك لتحويل الزائر إلى عميل فعلي،
        مع تركيز خاص على الحجز والتواصل عبر واتساب.
      </p>
    </div>
  </div>

  <div class="card">
    <h2>تفصيل المحاور</h2>
    {dim_rows}
  </div>

  <div class="card">
    <h2>أبرز نقاط القوة</h2>
    <ul class="good">{strengths_html}</ul>
  </div>

  <div class="card">
    <h2>مشاكل تستحق المعالجة</h2>
    <ul class="bad">{issues_html}</ul>
  </div>

  <div class="card">
    <h2>خطة تطوير على ٣ مراحل</h2>
    {plan_html}
  </div>

  <div class="card">
    <h2>كيف نساعدك؟ خدمتان جاهزتان</h2>
    <div class="offers">
      <div class="offer">
        <span class="pill">الخدمة الأولى</span>
        <h3>تطوير / بناء الموقع</h3>
        <p>موقع سريع، متوافق مع الجوال، مهيّأ لمحركات البحث، ومصمّم
           لتحويل الزائر إلى عميل — يعالج كل المشاكل أعلاه.</p>
      </div>
      <div class="offer">
        <span class="pill">الخدمة الثانية</span>
        <h3>نظام حجز + مساعد واتساب</h3>
        <p>حجز مواعيد ذاتي يعمل ٢٤ ساعة، ومساعد واتساب آلي يردّ على
           العملاء ويؤكّد الحجوزات تلقائياً.</p>
      </div>
    </div>
  </div>

  <div class="cta">
    <a class="btn" href="{_e(wa_link)}" target="_blank" rel="noopener">
      احجز استشارتك المجانية عبر واتساب
    </a>
  </div>

  <div class="foot">
    أُنشئ هذا التقرير بواسطة {_e(brand)} · {_e(gen_dt)}
  </div>

</div>
{pixel_tag}
</body>
</html>"""


# --------------------------------------------------------------------------- #
# اختبار ذاتي يثبت العقد                                                       #
# --------------------------------------------------------------------------- #
def _self_test() -> bool:
    print("=== اختبار محرّك التدقيق audit.py ===")
    ok_all = True

    # 1) العقد على موقع عام بسيط
    a = audit_site("https://example.com", timeout=15)
    print(f"[+] دُقّق https://example.com → ok={a['ok']} الدرجة={a['score']}/100 "
          f"عدد المشاكل={len(a['issues'])} عدد نقاط القوة={len(a['strengths'])}")

    # تحقّق من بنية العقد
    required = {"url", "ok", "score", "dims", "issues", "strengths",
                "has_booking", "has_whatsapp", "https", "mobile", "title", "desc"}
    missing = required - set(a.keys())
    if missing:
        print(f"[!] مفاتيح ناقصة في الناتج: {missing}"); ok_all = False
    if not (isinstance(a["score"], int) and 0 <= a["score"] <= 100):
        print(f"[!] الدرجة خارج النطاق: {a['score']}"); ok_all = False
    if set(a["dims"].keys()) != set(WEIGHTS.keys()):
        print(f"[!] محاور dims غير متطابقة: {a['dims'].keys()}"); ok_all = False
    for dk, dv in a["dims"].items():
        if not (0 <= dv <= 100):
            print(f"[!] درجة محور {dk} خارج النطاق: {dv}"); ok_all = False
    for fld in ("ok", "has_booking", "has_whatsapp", "https", "mobile"):
        if not isinstance(a[fld], bool):
            print(f"[!] الحقل {fld} ليس bool"); ok_all = False
    print(f"    المحاور: {a['dims']}")

    # 2) رابط معطوب يجب أن يفشل بأمان (ok=False, score=0, فيه مشاكل)
    bad = audit_site("http://no-such-domain-xyz-9931.invalid", timeout=6)
    if bad["ok"] or bad["score"] != 0 or not bad["issues"]:
        print(f"[!] الرابط المعطوب لم يُعالَج بأمان: ok={bad['ok']} score={bad['score']}")
        ok_all = False
    else:
        print(f"[+] رابط معطوب عولج بأمان: ok={bad['ok']} score={bad['score']} "
              f"مشاكل={len(bad['issues'])}")

    # 3) توليد تقرير لعمل وهمي والتحقق أنه HTML غير فارغ وبلا placeholders
    biz = {"name": "مطعم الذوق الرفيع", "phone": "0501234567",
           "website": "https://example.com"}
    report = render_report(biz, a, brand="واجهة")
    checks = {
        "غير فارغ": len(report) > 1500,
        "DOCTYPE": report.lstrip().lower().startswith("<!doctype html"),
        "RTL": 'dir="rtl"' in report,
        "noindex": "noindex" in report,
        "اسم العمل": "مطعم الذوق الرفيع" in report,
        "زر واتساب": "wa.me/" in report,
        "الخدمتان": ("تطوير" in report and "مساعد واتساب" in report),
        "خطة 3 مراحل": ("المرحلة الأولى" in report and
                        "المرحلة الثانية" in report and
                        "المرحلة الثالثة" in report),
        "الدرجة ظاهرة": str(a["score"]) in report,
    }
    # لا placeholders متبقية
    placeholders = ["{}", "{name}", "{score}", "TODO", "PLACEHOLDER",
                    "lorem", "xxxxx", "نص هنا"]
    for ph in placeholders:
        if ph in report:
            checks[f"بلا placeholder ({ph})"] = False

    for cname, cval in checks.items():
        print(f"    [{'✓' if cval else '✗'}] {cname}")
        if not cval:
            ok_all = False

    # احفظ عيّنة للمعاينة اليدوية
    try:
        with open("audit_report_sample.html", "w", encoding="utf-8") as fh:
            fh.write(report)
        print("[+] حُفظت عيّنة التقرير: audit_report_sample.html")
    except Exception as e:
        print(f"[i] تعذّر حفظ العيّنة (غير حرج): {e}")

    print("=== النتيجة:", "نجح ✓" if ok_all else "فشل ✗", "===")
    return ok_all


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="محرّك تدقيق موقع — واجهة")
    ap.add_argument("url", nargs="?", help="رابط الموقع للتدقيق (اختياري)")
    ap.add_argument("--name", default="منشأة تجريبية", help="اسم العمل للتقرير")
    ap.add_argument("--phone", default="0501234567", help="رقم الهاتف لزر واتساب")
    ap.add_argument("--out", default="audit_report.html", help="ملف التقرير الناتج")
    ap.add_argument("--timeout", type=int, default=15)
    args = ap.parse_args()

    if args.url:
        res = audit_site(args.url, timeout=args.timeout)
        print(f"الدرجة: {res['score']}/100 | المشاكل: {len(res['issues'])} | "
              f"حجز={res['has_booking']} واتساب={res['has_whatsapp']} "
              f"https={res['https']} جوال={res['mobile']}")
        page = render_report({"name": args.name, "phone": args.phone,
                              "website": res["url"]}, res)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(page)
        print(f"حُفظ التقرير: {args.out}")
    else:
        ok = _self_test()
        sys.exit(0 if ok else 1)
