"""
خط الإنتاج الكامل (Pipeline) — أمر واحد يربط كل المراحل
========================================================
    استخراج البيانات  →  ترتيب الفرص  →  تجهيز حِزَم العرض  →  (اختياري) استيراد لنظام الحجز

التشغيل:
    # استخرج وجهّز كل شيء دفعة واحدة:
    python pipeline.py --keyword "صالونات تجميل" --city "الرياض" --max 10

    # مع افتراضات العائد واستيراد المنشآت لنظام الحجز:
    python pipeline.py -k "عيادات أسنان" -c "جدة" -n 15 --ticket 200 --weekly-extra 12 --import-booking

    # من ملف موجود بدل السحب:
    python pipeline.py --input output/salons.json

المخرجات (في output/):
    • ملف JSON خام للبيانات المسحوبة
    • leads_*.csv / .html        (قائمة الفرص المرتّبة)
    • outreach/                  (حزمة عرض لكل منشأة + لوحة الحملة index.html)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import leads
import outreach


def _step(n, total, title):
    print(f"\n[{n}/{total}] {title}")
    print("-" * 50)


def main() -> None:
    ap = argparse.ArgumentParser(description="خط الإنتاج الكامل: استخراج → فرز → حِزَم عرض → حجز")
    ap.add_argument("--keyword", "-k", help="كلمة البحث (مثال: صالونات)")
    ap.add_argument("--city", "-c", help="المدينة (مثال: الرياض)")
    ap.add_argument("--max", "-n", type=int, default=10, help="أقصى عدد نتائج (افتراضي 10)")
    ap.add_argument("--input", "-i", nargs="*",
                    help="استخدم ملف/ملفات JSON موجودة بدل السحب")
    ap.add_argument("--lang", default="ar", help="لغة الواجهة (افتراضي ar)")
    ap.add_argument("--country", default="966", help="رمز الدولة للهاتف (افتراضي 966)")
    ap.add_argument("--no-headless", dest="headless", action="store_false", default=True,
                    help="افتح المتصفح مرئياً أثناء السحب")
    ap.add_argument("--ticket", type=float, default=100.0, help="متوسط الفاتورة لتقدير العائد")
    ap.add_argument("--weekly-extra", type=int, default=8, help="حجوزات إضافية أسبوعية مقدّرة")
    ap.add_argument("--import-booking", action="store_true",
                    help="استورد المنشآت إلى نظام الحجز بعد التجهيز")
    args = ap.parse_args()

    if not args.input and not (args.keyword and args.city):
        ap.error("حدّد --keyword و --city للسحب، أو --input لملف موجود.")

    total = 4 if args.import_booking else 3
    print("=" * 60)
    print(" 🚀 خط الإنتاج الكامل — Google Maps → حملة جاهزة")
    print("=" * 60)

    # --- 1) الحصول على البيانات ---------------------------------------- #
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.input:
        _step(1, total, f"تحميل البيانات من ملفات موجودة")
        records = leads.load_records(args.input)
        json_paths = args.input
        print(f"تم تحميل {len(records)} سجلاً.")
    else:
        _step(1, total, f"سحب «{args.keyword}» في «{args.city}» (حتى {args.max})")
        from scraper import GoogleMapsScraper, save_json
        out_json = Path("output") / f"pipeline_{args.keyword}_{args.city}_{stamp}.json"
        with GoogleMapsScraper(headless=args.headless, lang=args.lang) as s:
            places = s.search(args.keyword, args.city, args.max)
        save_json(places, out_json)
        records = [p.to_dict() for p in places]
        json_paths = [str(out_json)]
        print(f"تم سحب {len(records)} منشأة → {out_json}")

    if not records:
        print("\n❌ لا توجد بيانات. تأكّد من كلمة البحث/المدينة أو الملف.")
        sys.exit(1)

    # --- 2) ترتيب الفرص (leads) ---------------------------------------- #
    _step(2, total, "ترتيب الفرص (محرّك العملاء المحتملين)")
    L = leads.build_leads(records, args.country)
    leads_csv = Path("output") / f"leads_{stamp}.csv"
    leads_html = leads_csv.with_suffix(".html")
    leads.save_csv(L, leads_csv)
    leads.save_html(L, leads_html)
    n_no_site = sum(1 for x in L if x["needs_website"] == "نعم")
    print(f"رُتّبت {len(L)} منشأة · {n_no_site} بلا موقع · أعلى درجة: {L[0]['lead_score'] if L else 0}")

    # --- 3) تجهيز حِزَم العرض (outreach) -------------------------------- #
    _step(3, total, "تجهيز حِزَم العرض (صفحة + مقترح + رسالة لكل منشأة)")
    items = outreach.build_kits(records, "output/outreach", args.country,
                                args.ticket, args.weekly_extra)
    print(f"جُهّزت {len(items)} حزمة في: output/outreach")

    # --- 4) استيراد لنظام الحجز (اختياري) ------------------------------- #
    if args.import_booking:
        _step(4, total, "استيراد المنشآت إلى نظام الحجز")
        import types
        import booking_system as bs
        bs.cmd_import(types.SimpleNamespace(input=json_paths, country=args.country))

    # --- الملخّص النهائي ------------------------------------------------ #
    print("\n" + "=" * 60)
    print(" ✅ اكتمل خط الإنتاج")
    print("=" * 60)
    print(f"📊 قائمة الفرص   : {leads_html}")
    print(f"🎯 لوحة الحملة   : output/outreach/index.html  (افتحها وابدأ التواصل)")
    if args.import_booking:
        print(f"🗓️  نظام الحجز    : python booking_system.py run  →  http://localhost:5001")
    print("\nالخطوة التالية: افتح لوحة الحملة → «نسخ الرسالة» أو «واتساب» لكل منشأة.")


if __name__ == "__main__":
    main()
