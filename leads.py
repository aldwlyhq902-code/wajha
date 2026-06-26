"""
محرّك العملاء المحتملين (Leads Engine)
=======================================
يحوّل مخرجات السحب (JSON من مجلد output/) إلى قائمة فرص بيع مرتّبة بالأولوية:

  • needs_website  : هل المنشأة بلا موقع إلكتروني؟ (أهم إشارة فرصة)
  • lead_score     : درجة الفرصة 0–100 (تقييم × مراجعات × تواصل × فئة × غياب موقع)
  • whatsapp_link  : رابط واتساب جاهز برسالة عرض (للأرقام الجوّالة)
  • call_link      : رابط اتصال tel:
  • suggested_action: الإجراء المقترح لكل منشأة

التشغيل:
    python leads.py                       # يقرأ كل ملفات output/*.json
    python leads.py --input output/x.json  # ملف/ملفات محددة (يدعم النمط *)
    python leads.py --country 966 --out output/leads.csv

يُنتج:  output/leads_<وقت>.csv   (لإكسل)  و  output/leads_<وقت>.html  (للعرض)
"""

from __future__ import annotations

import argparse
import csv
import glob
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# على Windows: أجبر UTF-8 لتجنّب UnicodeEncodeError عند طباعة العربية
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# الهاتف: تطبيع دولي + كشف الجوّال                                            #
# --------------------------------------------------------------------------- #
def normalize_phone(raw: str | None, default_cc: str = "966") -> str | None:
    """حوّل الرقم إلى صيغة دولية بأرقام فقط (بدون + ولا مسافات)."""
    if not raw:
        return None
    has_plus = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if has_plus:
        return digits                      # يحوي رمز الدولة أصلاً
    if digits.startswith("00"):
        return digits[2:]                  # بادئة دولية 00
    if digits.startswith("0"):
        return default_cc + digits[1:]     # محلي: استبدل 0 برمز الدولة
    if digits.startswith(default_cc):
        return digits
    return default_cc + digits             # افتراض: محلي بلا 0


def is_saudi_mobile(intl: str | None, default_cc: str = "966") -> bool:
    """جوّال سعودي = ٩ أرقام تبدأ بـ 5 بعد رمز الدولة."""
    if not intl:
        return False
    nat = intl[len(default_cc):] if intl.startswith(default_cc) else intl
    return default_cc == "966" and len(nat) == 9 and nat.startswith("5")


def whatsappable(intl: str | None, raw: str | None, default_cc: str) -> bool:
    """قابل للواتساب: جوّال سعودي، أو رقم دولي (يبدأ بـ +) لغير السعودية."""
    if not intl:
        return False
    if is_saudi_mobile(intl, default_cc):
        return True
    # أرقام دولية (+) لدول أخرى: غالباً عليها واتساب أعمال
    return bool(raw and raw.strip().startswith("+") and not intl.startswith(default_cc))


# --------------------------------------------------------------------------- #
# تصنيف الفئة لأغراض الحجز                                                    #
# --------------------------------------------------------------------------- #
# فئات يُناسبها الحجز/المواعيد كثيراً → وزن أعلى
_HIGH_VALUE_CATS = (
    "مطعم", "مقهى", "كافيه", "عياد", "طبيب", "أسنان", "مستشفى", "صالون",
    "حلاق", "تجميل", "سبا", "مساج", "فندق", "نزل", "شقق", "منتجع",
    "ملعب", "صالة", "جيم", "نادي", "بيطري", "عقار", "قاعة", "كوفي",
    "restaurant", "cafe", "clinic", "dentist", "salon", "spa", "hotel",
    "gym", "barber", "vet",
)
_MID_VALUE_CATS = ("صيدلي", "متجر", "محل", "مكتب", "ورشة", "store", "shop", "pharmacy")


def category_weight(category: str) -> int:
    c = (category or "").lower()
    if any(k.lower() in c for k in _HIGH_VALUE_CATS):
        return 10
    if any(k.lower() in c for k in _MID_VALUE_CATS):
        return 5
    return 3


# --------------------------------------------------------------------------- #
# حساب درجة الفرصة + الأسباب                                                  #
# --------------------------------------------------------------------------- #
def score_lead(r: dict, intl: str | None, can_whatsapp: bool) -> tuple[int, str]:
    reasons: list[str] = []
    score = 0.0

    needs_website = not (r.get("website") or "").strip()
    if needs_website:
        score += 40
        reasons.append("بلا موقع")

    if r.get("phone"):
        score += 10
        reasons.append("لديه هاتف")
    if can_whatsapp:
        score += 10
        reasons.append("واتساب")

    reviews = r.get("reviews_count") or 0
    pop = min(25.0, reviews ** 0.5)        # شعبية بسلّم لوغاريتمي (حتى 25)
    score += pop
    if reviews >= 100:
        reasons.append(f"{reviews} مراجعة")

    rating = r.get("rating")
    if rating:
        score += (rating / 5.0) * 15       # جودة (حتى 15)
        if rating >= 4.3:
            reasons.append(f"تقييم {rating}")

    cw = category_weight(r.get("category", ""))
    score += cw
    if cw == 10:
        reasons.append("فئة مناسبة للحجز")

    return round(min(100.0, score)), "، ".join(reasons)


def suggested_action(r: dict, needs_website: bool, can_whatsapp: bool) -> str:
    if needs_website and can_whatsapp:
        return "أولوية عالية: بلا موقع — أرسل عرض صفحة الحجز عبر واتساب"
    if needs_website:
        return "بلا موقع — اتصل لعرض صفحة حجز إلكتروني"
    if can_whatsapp:
        return "لديه موقع — اعرض ترقية لنظام حجز/مواعيد عبر واتساب"
    return "لديه موقع — اتصل لعرض تحسين/نظام حجز"


# --------------------------------------------------------------------------- #
# رسالة العرض + روابط                                                         #
# --------------------------------------------------------------------------- #
def pitch_message(r: dict) -> str:
    name = r.get("name") or "منشأتكم"
    rating = r.get("rating")
    reviews = r.get("reviews_count")
    praise = ""
    if rating and reviews:
        praise = f" لاحظت تقييمكم الممتاز ({rating}⭐ و{reviews} مراجعة)،"
    return (
        f"السلام عليكم،{praise} وأنّ {name} بلا نظام حجز إلكتروني/موقع. "
        "أوفّر لكم صفحة حجز أونلاين تتيح لعملائكم الحجز مباشرة (مواعيد/طاولات) "
        "مع لوحة إدارة بسيطة. هل يناسبكم عرض سريع؟"
    )


def whatsapp_link(intl: str, msg: str) -> str:
    return f"https://wa.me/{intl}?text={quote(msg)}"


# --------------------------------------------------------------------------- #
# التحميل + إزالة التكرار                                                     #
# --------------------------------------------------------------------------- #
def _dedupe_key(r: dict) -> str:
    m = re.search(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", r.get("place_url", "") or "")
    if m:
        return m.group(0)
    return (r.get("name", "") + "|" + (r.get("phone", "") or "")).strip().lower()


def load_records(patterns: list[str]) -> list[dict]:
    files: list[str] = []
    for p in patterns:
        files.extend(glob.glob(p))
    records: list[dict] = []
    seen: set[str] = set()
    for f in files:
        try:
            data = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ تعذّر قراءة {f}: {e}", file=sys.stderr)
            continue
        if isinstance(data, dict):
            data = [data]
        for r in data:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            k = _dedupe_key(r)
            if k in seen:
                continue
            seen.add(k)
            records.append(r)
    return records


# --------------------------------------------------------------------------- #
# بناء صفوف الفرص                                                             #
# --------------------------------------------------------------------------- #
LEAD_FIELDS = [
    "lead_score", "name", "category", "rating", "reviews_count",
    "needs_website", "phone", "whatsapp_link", "call_link", "website",
    "suggested_action", "reasons", "address", "maps_url",
]


def build_leads(records: list[dict], country: str) -> list[dict]:
    leads: list[dict] = []
    for r in records:
        intl = normalize_phone(r.get("phone"), country)
        can_whatsapp = whatsappable(intl, r.get("phone"), country)
        needs_website = not (r.get("website") or "").strip()
        score, reasons = score_lead(r, intl, can_whatsapp)
        leads.append({
            "lead_score": score,
            "name": r.get("name", ""),
            "category": r.get("category", ""),
            "rating": r.get("rating"),
            "reviews_count": r.get("reviews_count"),
            "needs_website": "نعم" if needs_website else "لا",
            "phone": r.get("phone", ""),
            "whatsapp_link": whatsapp_link(intl, pitch_message(r)) if (intl and can_whatsapp) else "",
            "call_link": f"tel:+{intl}" if intl else "",
            "website": r.get("website", ""),
            "suggested_action": suggested_action(r, needs_website, can_whatsapp),
            "reasons": reasons,
            "address": r.get("address", ""),
            "maps_url": r.get("place_url", ""),
        })
    leads.sort(key=lambda x: x["lead_score"], reverse=True)
    return leads


# --------------------------------------------------------------------------- #
# الإخراج: CSV + HTML                                                         #
# --------------------------------------------------------------------------- #
def save_csv(leads: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEAD_FIELDS)
        w.writeheader()
        for L in leads:
            w.writerow(L)


def save_html(leads: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_no_site = sum(1 for L in leads if L["needs_website"] == "نعم")

    def esc(s):
        return html.escape(str(s if s is not None else ""))

    rows = []
    for L in leads:
        wa = (f'<a class="wa" href="{esc(L["whatsapp_link"])}" target="_blank">واتساب</a>'
              if L["whatsapp_link"] else "")
        call = (f'<a class="call" href="{esc(L["call_link"])}">اتصال</a>'
                if L["call_link"] else "")
        site = (f'<a href="{esc(L["website"])}" target="_blank">موقع</a>'
                if L["website"] else '<span class="no">— لا يوجد</span>')
        mapl = (f'<a href="{esc(L["maps_url"])}" target="_blank">خريطة</a>'
                if L["maps_url"] else "")
        badge = "hi" if L["lead_score"] >= 70 else ("mid" if L["lead_score"] >= 50 else "lo")
        rows.append(f"""<tr>
  <td><span class="score {badge}">{L['lead_score']}</span></td>
  <td><strong>{esc(L['name'])}</strong><div class="sub">{esc(L['category'])}</div></td>
  <td>{esc(L['rating']) if L['rating'] else '—'}</td>
  <td>{esc(L['reviews_count']) if L['reviews_count'] else '—'}</td>
  <td>{site}</td>
  <td>{esc(L['phone'])}</td>
  <td class="actions">{wa} {call} {mapl}</td>
  <td class="addr">{esc(L['suggested_action'])}</td>
</tr>""")

    doc = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>قائمة العملاء المحتملين</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:"Segoe UI",Tahoma,sans-serif}}
body{{background:#f1f3f4;color:#202124;padding:24px;font-size:14px}}
h1{{font-size:22px;margin-bottom:4px}}
.muted{{color:#5f6368;margin-bottom:18px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}}
.kpi{{background:#fff;border:1px solid #dadce0;border-radius:12px;padding:14px 18px;min-width:150px}}
.kpi b{{font-size:26px;display:block;color:#4285f4}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dadce0;border-radius:12px;overflow:hidden}}
th,td{{padding:11px 12px;text-align:right;border-bottom:1px solid #eee;vertical-align:middle}}
th{{background:#f8f9fa;color:#5f6368;font-size:13px}}
tr:hover{{background:#f8f9fa}}
.score{{display:inline-block;min-width:34px;text-align:center;padding:4px 8px;border-radius:20px;font-weight:700;color:#fff}}
.score.hi{{background:#34a853}} .score.mid{{background:#fbbc04;color:#202124}} .score.lo{{background:#9aa0a6}}
.sub{{font-size:12px;color:#5f6368}}
.no{{color:#ea4335}}
.actions a{{display:inline-block;margin-left:6px;text-decoration:none;font-size:13px;padding:4px 10px;border-radius:6px}}
.wa{{background:#25d366;color:#fff!important}} .call{{background:#4285f4;color:#fff!important}}
.addr{{max-width:260px;color:#5f6368}}
</style></head><body>
<h1>🎯 قائمة العملاء المحتملين</h1>
<p class="muted">مرتّبة بدرجة الفرصة. الأخضر = أولوية عالية. اضغط «واتساب» لإرسال عرض جاهز.</p>
<div class="cards">
  <div class="kpi"><b>{len(leads)}</b> إجمالي المنشآت</div>
  <div class="kpi"><b>{n_no_site}</b> بلا موقع (فرص)</div>
  <div class="kpi"><b>{sum(1 for L in leads if L['lead_score']>=70)}</b> أولوية عالية</div>
</div>
<table><thead><tr>
<th>الدرجة</th><th>المنشأة</th><th>التقييم</th><th>المراجعات</th><th>الموقع</th>
<th>الهاتف</th><th>إجراءات</th><th>الإجراء المقترح</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
</body></html>"""
    path.write_text(doc, encoding="utf-8")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="محرّك العملاء المحتملين من بيانات Google Maps")
    ap.add_argument("--input", "-i", nargs="*", default=["output/*.json"],
                    help="ملف/ملفات JSON (يدعم النمط *). الافتراضي: output/*.json")
    ap.add_argument("--country", "-c", default="966", help="رمز الدولة للهاتف (افتراضي 966)")
    ap.add_argument("--out", "-o", help="مسار CSV الناتج (يُشتق منه HTML)")
    args = ap.parse_args()

    records = load_records(args.input)
    if not records:
        print("❌ لا توجد سجلات. شغّل السحب أولاً (run.py / app.py) ثم أعد المحاولة.")
        sys.exit(1)

    leads = build_leads(records, args.country)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = Path(args.out) if args.out else Path("output") / f"leads_{stamp}.csv"
    html_path = csv_path.with_suffix(".html")
    save_csv(leads, csv_path)
    save_html(leads, html_path)

    # ملخّص في الطرفية
    n_no_site = sum(1 for L in leads if L["needs_website"] == "نعم")
    n_hi = sum(1 for L in leads if L["lead_score"] >= 70)
    print("=" * 60)
    print(" 🎯 محرّك العملاء المحتملين")
    print("=" * 60)
    print(f"إجمالي المنشآت      : {len(leads)}")
    print(f"بلا موقع (فرص بيع)  : {n_no_site}")
    print(f"أولوية عالية (≥70)  : {n_hi}")
    print("\n--- أعلى 10 فرص ---")
    for L in leads[:10]:
        wa = "📲واتساب" if L["whatsapp_link"] else "📞اتصال"
        site = "بلا موقع" if L["needs_website"] == "نعم" else "لديه موقع"
        print(f"  [{L['lead_score']:>3}] {L['name'][:32]:<32} | {site:<9} | {wa} | {L['reasons']}")
    print(f"\n✓ CSV : {csv_path}")
    print(f"✓ HTML: {html_path}  (افتحه في المتصفح)")


if __name__ == "__main__":
    main()
