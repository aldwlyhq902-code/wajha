"""
offers.py — بناء رسائل العرض (تقرير الموقع / موقع جديد + خدمات whats_bot).
=============================================================================
وحدة نقيّة (stdlib فقط) تبني نصّ الرسالة التسويقية الفرديّة المناسبة لكل عميل
حسب نوع العرض وفئته، مع إدراج خدمات «whats_bot» الملائمة لنشاطه.

العقد العام:
    whatsbot_services(category) -> list[str]
    build_message(prospect: dict, offer: str, report_link: str = "", brand="واجهة") -> str
    email_subject(prospect: dict, offer: str) -> str

`offer` ∈ {"report", "newsite"}:
  • report  : «شاهد تقرير موقعك» — مناسب لمن لديه موقع (نعرض تدقيقه + التطوير).
  • newsite : «موقع جديد + حجز واتساب» — مناسب لمن بلا موقع (أو موقع ضعيف).

التزامًا بنظام حماية البيانات (PDPL): رسائل فرديّة مهنيّة فقط، ويُحترم طلب التوقف.
"""

from __future__ import annotations

import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# خدمات whats_bot المناسبة لكل فئة نشاط                                         #
# --------------------------------------------------------------------------- #
_CLINIC = ("عياد", "أسنان", "طبيب", "مستشفى", "طب", "مختبر", "clinic", "dental", "medical")
_SALON = ("صالون", "حلاق", "تجميل", "سبا", "مساج", "salon", "spa", "barber", "beauty")
_FOOD = ("مطعم", "مقهى", "كافيه", "كوفي", "قهوة", "مأكولات", "برجر", "بيتزا", "cafe", "restaurant")
_HOTEL = ("فندق", "نزل", "شقق", "منتجع", "استراحة", "hotel", "resort", "suites")

# خدمات عامّة تصلح لأي نشاط
_GENERIC_SERVICES = [
    "ردّ آليّ ذكيّ على استفسارات العملاء ٢٤/٧",
    "حجز/طلب داخل محادثة واتساب مباشرةً",
    "تذكير تلقائيّ بالمواعيد لتقليل الغياب",
    "حملات عروض لعملائك بضوابط تحمي رقمك من الحظر",
]

# خدمات مخصّصة حسب الفئة (تُدمج مع العامّة)
_BY_CATEGORY = {
    "clinic": [
        "حجز المواعيد الطبيّة تلقائيًّا مع تأكيد فوري",
        "تذكير المريض قبل الموعد وخفض نسبة الغياب",
        "أسئلة ما قبل الكشف وفرز الحالات آليًّا",
    ],
    "salon": [
        "حجز الخدمة (قصّ/صبغة/عناية) واختيار الوقت داخل واتساب",
        "تذكير العميلة قبل الموعد وعروض الولاء",
        "قائمة الخدمات والأسعار تردّ تلقائيًّا",
    ],
    "food": [
        "استقبال الطلبات والحجوزات عبر واتساب آليًّا",
        "قائمة الطعام والعروض ردًّا فوريًّا",
        "حملات عروض نهاية الأسبوع لعملائك",
    ],
    "hotel": [
        "حجز الغرف/الوحدات والاستعلام عن التوفّر تلقائيًّا",
        "تأكيد الحجز وتعليمات الوصول آليًّا",
        "عروض الإقامة المطوّلة لعملائك",
    ],
}


def _category_key(category: str) -> str:
    c = (category or "").lower()
    if any(k in c for k in _CLINIC):
        return "clinic"
    if any(k in c for k in _SALON):
        return "salon"
    if any(k in c for k in _FOOD):
        return "food"
    if any(k in c for k in _HOTEL):
        return "hotel"
    return "generic"


def whatsbot_services(category: str) -> list[str]:
    """قائمة خدمات whats_bot المناسبة للفئة (مخصّصة + عامّة، دون تكرار)."""
    key = _category_key(category)
    specific = _BY_CATEGORY.get(key, [])
    out: list[str] = []
    for s in specific + _GENERIC_SERVICES:
        if s not in out:
            out.append(s)
    return out[:5]


# --------------------------------------------------------------------------- #
# بناء نصّ الرسالة                                                             #
# --------------------------------------------------------------------------- #
def _name_of(prospect: dict) -> str:
    return (prospect.get("name") or "منشأتكم").strip()


def build_message(prospect: dict, offer: str, report_link: str = "",
                  brand: str = "واجهة") -> str:
    """يبني نصّ رسالة فرديّة مهنيّة حسب نوع العرض وفئة النشاط.

    offer="report"  : لمن لديه موقع — نعرض تدقيق موقعه + خطة تطوير + خدمات whats_bot.
    offer="newsite" : لمن بلا موقع — نعرض بناء موقع جديد + حجز واتساب عبر whats_bot.
    """
    prospect = prospect or {}
    name = _name_of(prospect)
    category = prospect.get("category") or ""
    services = whatsbot_services(category)
    services_block = "\n".join(f"• {s}" for s in services[:4])

    optout = "\n\n— للإيقاف ردّوا بكلمة: إيقاف"

    if offer == "report":
        head = (
            f"مرحباً {name} 👋\n"
            f"أعددنا لكم *تقريراً مجانياً* يقيس جاهزية موقعكم الحاليّ لتحويل الزائر إلى عميل "
            f"(الحجز، التواصل، الظهور في البحث، توافق الجوال)."
        )
        link_line = f"\n📄 شاهدوا التقرير: {report_link}" if report_link else ""
        tail = (
            f"\n\nومع تطوير الموقع نضيف *مساعد واتساب آليّ (whats_bot)*:\n{services_block}\n\n"
            f"نسعد بشرحها لكم خلال دقيقتين. متى يناسبكم؟ — فريق {brand}"
        )
        return head + link_line + tail + optout

    # newsite (الافتراضي لمن بلا موقع)
    head = (
        f"مرحباً {name} 👋\n"
        f"لاحظنا أن منشأتكم بلا موقع إلكترونيّ يستقبل عملاءكم ويحجز لهم آليًّا. "
        f"نقترح *موقعاً احترافياً سريعاً* + *مساعد واتساب آليّ (whats_bot)*:"
    )
    body = f"\n{services_block}"
    link_line = f"\n\n👀 نموذج جاهز لمنشأتكم: {report_link}" if report_link else ""
    tail = f"\n\nنفعّلها باسمكم خلال يوم واحد. نسعد بخدمتكم — فريق {brand}"
    return head + body + link_line + tail + optout


def email_subject(prospect: dict, offer: str) -> str:
    name = _name_of(prospect)
    if offer == "report":
        return f"تقرير موقع {name} المجاني + كيف نرفع حجوزاتكم"
    return f"موقع إلكترونيّ + حجز واتساب لـ {name}"


# --------------------------------------------------------------------------- #
# اختبار ذاتيّ                                                                 #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    assert _category_key("عيادة أسنان") == "clinic"
    assert _category_key("صالون تجميل") == "salon"
    assert _category_key("مطعم برجر") == "food"
    assert _category_key("متجر هدايا") == "generic"
    svc = whatsbot_services("عيادة أسنان")
    assert any("موعد" in s or "المواعيد" in s for s in svc), svc
    m1 = build_message({"name": "عيادة النور", "category": "عيادة"}, "report",
                       "https://x.test/report/5?t=abc")
    assert "عيادة النور" in m1 and "التقرير" in m1 and "whats_bot" in m1 and "report/5" in m1
    m2 = build_message({"name": "صالون لمسة", "category": "صالون"}, "newsite")
    assert "صالون لمسة" in m2 and "whats_bot" in m2
    assert "تقرير" in email_subject({"name": "x"}, "report")
    print("PASS: offers.py — كل الاختبارات نجحت")
    print("---- مثال تقرير ----\n" + m1)
    print("---- مثال موقع جديد ----\n" + m2)
