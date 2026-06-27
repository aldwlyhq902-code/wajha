"""
محرّك التسعير الديناميكي (Dynamic Pricing Engine)
==================================================
محرّك نقيّ (بلا أي إدخال/إخراج ولا حالة) يقترح باقة اشتراك مناسبة لكل منشأة
بناءً على إشارات واقعية مستخلَصة من السحب والتدقيق:

  • audit_score : درجة فحص الموقع (0–100). الأقل = حاجة أكبر = قابلية بيع أعلى.
  • reviews     : عدد المراجعات. الأكثر = منشأة أكبر/أنشط = قدرة دفع أعلى.
  • category    : الفئة. فئات عالية القيمة (أسنان/تجميل/عيادات/فنادق) ترفع الباقة.

الفلسفة:
  - «الحاجة» (درجة موقع منخفضة أو غيابه) تبرّر البيع لكنها لا ترفع السعر كثيراً.
  - «الحجم» (مراجعات كثيرة) و«قيمة الفئة» هما ما يرفع الباقة والسعر فعلاً.

العقد العام:
    suggest_plan(audit_score, reviews, category, country="966") -> dict
    tier_for(audit_score, reviews, category)                    -> str

كل الأسعار قابلة للضبط عبر الثوابت أعلى الملف.
"""

from __future__ import annotations

import sys

# على Windows: أجبر UTF-8 لتجنّب UnicodeEncodeError عند طباعة العربية
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# ثوابت قابلة للضبط                                                           #
# --------------------------------------------------------------------------- #
# اسم الباقة الداخلي (المفتاح) -> أسماؤها العربية وأسعارها الإرشادية بالريال.
TIERS = ("basic", "pro", "premium")
TIER_NAMES_AR = {
    "basic":   "أساسية",
    "pro":     "احترافية",
    "premium": "متقدّمة",
}

# الأسعار الأساسية الإرشادية (ريال سعودي) — شهري ورسوم تأسيس لمرّة واحدة.
BASE_MONTHLY = {
    "basic":    99,
    "pro":     199,
    "premium": 399,
}
BASE_SETUP = {
    "basic":   199,
    "pro":     399,
    "premium": 799,
}

# حدود تقريب السعر النهائي لكل باقة (لئلّا يخرج التعديل عن المعقول).
MONTHLY_FLOOR = {"basic": 79,  "pro": 159, "premium": 319}
MONTHLY_CEIL  = {"basic": 149, "pro": 299, "premium": 599}

# عتبات الإشارات.
LOW_SCORE = 45          # درجة موقع ≤ هذه = حاجة قوية (نقطة بيع)
HIGH_SCORE = 75         # درجة موقع ≥ هذه = موقعه جيّد أصلاً
REVIEWS_SMALL = 50      # منشأة صغيرة/جديدة
REVIEWS_MID = 300       # منشأة متوسطة
REVIEWS_BIG = 1000      # منشأة كبيرة/راسخة

# تقريب السعر النهائي لأقرب مضاعف (أناقة تجارية: ...9, ...49).
ROUND_TO = 10

# فئات عالية القيمة: قطاعات تدفع جيّداً ويناسبها الحجز/المواعيد.
_HIGH_VALUE_CATS = (
    "عياد", "طبيب", "أسنان", "مستشفى", "تجميل", "جلدية", "تقويم", "زراعة",
    "صالون", "سبا", "مساج", "فندق", "نزل", "منتجع", "شقق",
    "clinic", "dentist", "dental", "derma", "cosmetic", "aesthetic",
    "salon", "spa", "hotel", "resort", "medical",
)
# فئات متوسطة القيمة.
_MID_VALUE_CATS = (
    "مطعم", "مقهى", "كافيه", "كوفي", "حلاق", "بيطري", "جيم", "نادي", "صالة",
    "عقار", "قاعة", "ملعب", "ورشة", "مركز",
    "restaurant", "cafe", "coffee", "barber", "vet", "gym", "fitness",
    "real estate", "hall", "studio",
)


def category_value(category: str) -> int:
    """قيمة الفئة: 2 عالية، 1 متوسطة، 0 عادية."""
    c = (category or "").lower()
    if any(k.lower() in c for k in _HIGH_VALUE_CATS):
        return 2
    if any(k.lower() in c for k in _MID_VALUE_CATS):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# منطق اختيار الباقة                                                          #
# --------------------------------------------------------------------------- #
def _need_points(audit_score: int | None) -> int:
    """نقاط «الحاجة» من درجة الموقع: غيابه/ضعفه يدفع نحو ترقية الباقة قليلاً."""
    if audit_score is None:
        return 2                      # بلا موقع إطلاقاً = حاجة قصوى
    if audit_score <= LOW_SCORE:
        return 2
    if audit_score < HIGH_SCORE:
        return 1
    return 0                          # موقعه جيّد أصلاً = حاجة أقل


def _size_points(reviews: int | None) -> int:
    """نقاط «الحجم» من المراجعات: المنشأة الأكبر تدفع أكثر."""
    r = reviews or 0
    if r >= REVIEWS_BIG:
        return 3
    if r >= REVIEWS_MID:
        return 2
    if r >= REVIEWS_SMALL:
        return 1
    return 0


def tier_for(audit_score: int | None, reviews: int | None, category: str) -> str:
    """
    يحدّد مفتاح الباقة (basic|pro|premium) بدمج ثلاث إشارات:
      - الحجم (المراجعات)      : المحرّك الأساسي لقدرة الدفع.
      - قيمة الفئة             : قطاعات راقية ترفع الباقة.
      - الحاجة (درجة الموقع)   : دافع بيع، يرفع الباقة درجة واحدة كحدّ.
    منطق شفّاف: نجمع نقاطاً ثم نسقطها على ثلاث شرائح.
    """
    points = _size_points(reviews) + category_value(category) + _need_points(audit_score)
    # المدى النظري 0..7. شرائح واضحة:
    if points >= 5:
        return "premium"
    if points >= 2:
        return "pro"
    return "basic"


# --------------------------------------------------------------------------- #
# تعديل السعر داخل الباقة                                                     #
# --------------------------------------------------------------------------- #
def _round_price(value: float, base: int) -> int:
    """قرّب لأقرب مضاعف ثم اطرح 1 لإبقاء نهاية ...9 إن كان الأساس كذلك."""
    nearest = int(round(value / ROUND_TO) * ROUND_TO)
    if base % ROUND_TO == (ROUND_TO - 1):     # الأساس ينتهي بـ 9
        nearest = max(ROUND_TO - 1, nearest - 1)
    return nearest


def _adjust_monthly(tier: str, audit_score: int | None,
                    reviews: int | None, cat_val: int) -> int:
    """
    يحرّك السعر الشهري ±داخل الباقة حسب الإشارات:
      + مراجعات ضخمة جداً، + فئة عالية القيمة  → أعلى.
      + موقعه جيّد أصلاً (درجة عالية)           → أقل قليلاً (إقناع أسهل بسعر ألطف).
    ثم يُقصّ ضمن [floor, ceil] الخاصّين بالباقة.
    """
    price = float(BASE_MONTHLY[tier])
    r = reviews or 0

    # حجم استثنائي يرفع السعر تدريجياً.
    if r >= REVIEWS_BIG:
        price *= 1.10
    if r >= 2 * REVIEWS_BIG:
        price *= 1.05

    # قيمة الفئة ترفع السعر.
    if cat_val == 2:
        price *= 1.10
    elif cat_val == 1:
        price *= 1.03

    # موقعه قويّ أصلاً: خصم لطيف يسهّل الإقناع (القيمة المضافة أقلّ إلحاحاً).
    if audit_score is not None and audit_score >= HIGH_SCORE:
        price *= 0.92

    price = max(MONTHLY_FLOOR[tier], min(MONTHLY_CEIL[tier], price))
    return _round_price(price, BASE_MONTHLY[tier])


def _setup_fee(tier: str, audit_score: int | None) -> int:
    """رسوم التأسيس: ترتفع عند غياب الموقع (إنشاء من الصفر) وتنخفض إن كان موقعه جيّداً."""
    fee = float(BASE_SETUP[tier])
    if audit_score is None:
        fee *= 1.25                  # بناء حضور رقمي من الصفر
    elif audit_score >= HIGH_SCORE:
        fee *= 0.75                  # أساس جاهز، تأسيس أخفّ
    return _round_price(fee, BASE_SETUP[tier])


# --------------------------------------------------------------------------- #
# الإضافات والمبرّرات                                                         #
# --------------------------------------------------------------------------- #
def _addons(tier: str, audit_score: int | None,
            reviews: int | None, cat_val: int) -> list[str]:
    out: list[str] = []
    r = reviews or 0

    # فئات الحجز/المواعيد: العربون يقلّل عدم الحضور.
    if cat_val == 2:
        out.append("تفعيل العربون لتقليل الحجوزات الوهمية")
        out.append("تذكير تلقائي بالموعد عبر واتساب")
    elif cat_val == 1:
        out.append("نظام حجز/طابور إلكتروني")

    # منشأة كبيرة وحاضرة: استثمر في النمو والإعلانات.
    if r >= REVIEWS_MID:
        out.append("حملة إعلانية موجّهة (جوجل/إنستغرام)")
        out.append("إدارة السمعة وجمع التقييمات تلقائياً")
    elif r >= REVIEWS_SMALL:
        out.append("باقة تقييمات: دعوة العملاء للتقييم بعد الزيارة")

    # حضور رقمي ضعيف أو غائب: ابدأ بالأساسيات.
    if audit_score is None:
        out.append("إنشاء صفحة هبوط/حجز احترافية")
        out.append("توثيق وتحسين بطاقة خرائط جوجل")
    elif audit_score <= LOW_SCORE:
        out.append("تحسين سرعة وأداء الموقع الحالي")

    if tier == "premium":
        out.append("ربط مع نظام نقاط البيع/الفوترة")

    # أزل التكرار مع الحفاظ على الترتيب.
    seen: set[str] = set()
    uniq: list[str] = []
    for a in out:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def _rationale(tier: str, audit_score: int | None,
               reviews: int | None, cat_val: int) -> str:
    parts: list[str] = []
    r = reviews or 0

    # إشارة الموقع.
    if audit_score is None:
        parts.append("لا يوجد موقع إلكتروني، والحاجة لحضور رقمي قصوى")
    elif audit_score <= LOW_SCORE:
        parts.append(f"درجة الموقع منخفضة ({audit_score}/100) وتدلّ على فرصة تحسين كبيرة")
    elif audit_score >= HIGH_SCORE:
        parts.append(f"موقعه الحالي جيّد ({audit_score}/100)، فالعرض ترقية لا تأسيس")
    else:
        parts.append(f"درجة الموقع متوسّطة ({audit_score}/100)")

    # إشارة الحجم.
    if r >= REVIEWS_BIG:
        parts.append(f"منشأة راسخة بعدد مراجعات كبير ({r}) يعكس حجم عملاء عالياً")
    elif r >= REVIEWS_MID:
        parts.append(f"حجم نشاط جيّد ({r} مراجعة)")
    elif r >= REVIEWS_SMALL:
        parts.append(f"نشاط ناشئ ({r} مراجعة)")
    elif r > 0:
        parts.append(f"عدد مراجعات محدود ({r})")
    else:
        parts.append("لا توجد مراجعات بعد")

    # إشارة الفئة.
    if cat_val == 2:
        parts.append("فئتها عالية القيمة (قطاع طبي/تجميلي/ضيافة) تبرّر باقة أعلى")
    elif cat_val == 1:
        parts.append("فئتها مناسبة للحجز والمواعيد")

    name = TIER_NAMES_AR[tier]
    return f"اقتراح الباقة «{name}»: " + "؛ ".join(parts) + "."


# --------------------------------------------------------------------------- #
# الواجهة العامة                                                              #
# --------------------------------------------------------------------------- #
def suggest_plan(audit_score: int | None, reviews: int | None,
                 category: str, country: str = "966") -> dict:
    """
    يقترح باقة تسعير ديناميكية لمنشأة.

    المدخلات:
      audit_score : درجة فحص الموقع 0–100 أو None (لا موقع/غير مفحوص).
      reviews     : عدد المراجعات أو None.
      category    : نص الفئة (عربي/إنجليزي).
      country     : رمز الدولة (افتراضي 966 = السعودية، العملة SAR).

    المخرجات (dict):
      plan      : اسم الباقة بالعربية (أساسية|احترافية|متقدّمة).
      monthly   : السعر الشهري (ريال، int).
      setup     : رسوم التأسيس لمرّة واحدة (ريال، int).
      currency  : "SAR".
      rationale : نص عربي يشرح سبب الاقتراح.
      addons    : قائمة اقتراحات إضافية (عربون/حملات/تقييمات...).
    """
    # تطبيع المدخلات دفاعياً.
    try:
        score = int(audit_score) if audit_score is not None else None
    except (TypeError, ValueError):
        score = None
    if score is not None:
        score = max(0, min(100, score))

    try:
        rev = int(reviews) if reviews is not None else None
    except (TypeError, ValueError):
        rev = None
    if rev is not None:
        rev = max(0, rev)

    cat = category or ""
    cat_val = category_value(cat)

    tier = tier_for(score, rev, cat)
    monthly = _adjust_monthly(tier, score, rev, cat_val)
    setup = _setup_fee(tier, score)

    return {
        "plan": TIER_NAMES_AR[tier],
        "monthly": monthly,
        "setup": setup,
        "currency": "SAR",
        "rationale": _rationale(tier, score, rev, cat_val),
        "addons": _addons(tier, score, rev, cat_val),
    }


# --------------------------------------------------------------------------- #
# اختبار ذاتي يثبت العقد                                                      #
# --------------------------------------------------------------------------- #
def _selftest() -> None:
    cases = [
        ("عيادة أسنان (موقع ضعيف، مراجعات ضخمة)",
         dict(audit_score=40, reviews=1900, category="عيادة أسنان")),
        ("صالون (موقع متوسّط، مراجعات معتدلة)",
         dict(audit_score=70, reviews=120, category="صالون تجميل")),
        ("منشأة بلا موقع (غير مفحوصة)",
         dict(audit_score=None, reviews=None, category="مطعم")),
    ]

    results = []
    print("=" * 70)
    print("اختبار محرّك التسعير الديناميكي")
    print("=" * 70)
    for label, kw in cases:
        plan = suggest_plan(**kw)
        results.append(plan)
        print(f"\n• {label}")
        print(f"  المدخلات : {kw}")
        print(f"  الباقة   : {plan['plan']}")
        print(f"  الشهري   : {plan['monthly']} {plan['currency']}")
        print(f"  التأسيس  : {plan['setup']} {plan['currency']}")
        print(f"  المبرّر  : {plan['rationale']}")
        print(f"  إضافات   : {plan['addons']}")

    # تحقّق من العقد: المفاتيح والأنواع.
    required = {"plan", "monthly", "setup", "currency", "rationale", "addons"}
    for p in results:
        assert required <= set(p), f"مفاتيح ناقصة: {required - set(p)}"
        assert p["plan"] in TIER_NAMES_AR.values(), f"اسم باقة غير صالح: {p['plan']}"
        assert isinstance(p["monthly"], int) and p["monthly"] > 0
        assert isinstance(p["setup"], int) and p["setup"] >= 0
        assert p["currency"] == "SAR"
        assert isinstance(p["rationale"], str) and p["rationale"].strip()
        assert isinstance(p["addons"], list)

    dental, salon, nosite = results

    # تحقّق منطقي: عيادة أسنان ضخمة المراجعات يجب أن تكون «متقدّمة».
    assert dental["plan"] == "متقدّمة", f"عيادة الأسنان يجب أن تكون متقدّمة، صارت {dental['plan']}"
    # الصالون المتوسّط يجب أن يكون أقلّ من المتقدّمة.
    assert salon["plan"] != "متقدّمة", f"الصالون لا يجب أن يكون متقدّماً، صار {salon['plan']}"
    # الباقات الثلاث مختلفة منطقياً (الأسنان أعلى من الصالون).
    order = {"أساسية": 0, "احترافية": 1, "متقدّمة": 2}
    assert order[dental["plan"]] > order[salon["plan"]], \
        "ترتيب الباقات غير منطقي بين الأسنان والصالون"
    # عيادة الأسنان (فئة عالية + مراجعات ضخمة) أغلى شهرياً من الصالون.
    assert dental["monthly"] > salon["monthly"], "سعر الأسنان يجب أن يفوق الصالون"
    # منشأة بلا موقع يجب أن تتضمّن إضافة إنشاء حضور رقمي.
    assert any("إنشاء" in a or "بطاقة" in a for a in nosite["addons"]), \
        "منشأة بلا موقع يجب أن تقترح إنشاء حضور رقمي"
    # العربون يجب أن يُقترح لعيادة الأسنان (فئة عالية القيمة).
    assert any("عربون" in a for a in dental["addons"]), "الأسنان يجب أن تقترح العربون"

    print("\n" + "=" * 70)
    print("نجح الاختبار الذاتي: العقد ثابت والباقات تختلف منطقياً.")
    print("=" * 70)


if __name__ == "__main__":
    _selftest()
