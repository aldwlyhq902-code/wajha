"""
نقطة دخول أداة سحب بيانات Google Maps.

طرق الاستخدام:
    # 1) بحث بكلمة ومدينة
    python run.py search --keyword "مطاعم" --city "القاهرة" --max 30

    # 2) رابط موقع محدد
    python run.py url --url "https://www.google.com/maps/place/..."

    # 3) قائمة روابط/أسماء من ملف (سطر لكل عنصر)
    python run.py file --input input_list.txt --max 50

خيارات عامة:
    --headless / --no-headless   تشغيل المتصفح مخفياً أو ظاهراً (افتراضي: مخفي)
    --lang LANG                  لغة الواجهة (افتراضي: ar)
    --output PATH                مسار ملف JSON الناتج
    --csv PATH                   حفظ نسخة CSV إضافية (اختياري)
    --proxy "server=host:port;username=u;password=p"   بروكسي اختياري
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# على Windows، الطرفية تستخدم cp1252 ولا تطبع العربية/الرموز → UnicodeEncodeError.
# نُجبر مخرجات UTF-8 قبل أي طباعة أو إعداد للسجلات.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from scraper import GoogleMapsScraper, Place, save_json, save_csv


def _parse_proxy(text: str | None) -> dict | None:
    if not text:
        return None
    parts = [p.strip() for p in text.split(";") if p.strip()]
    proxy = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            proxy[k.strip()] = v.strip()
        else:
            proxy.setdefault("server", p)
    return proxy or None


def _default_output(name_hint: str) -> Path:
    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return out_dir / f"{name_hint}_{stamp}.json"


def _run_scraper(args, scraper_fn):
    scraper = GoogleMapsScraper(
        headless=args.headless,
        lang=args.lang,
        proxy=_parse_proxy(args.proxy),
    )
    try:
        with scraper:
            return scraper_fn(scraper)
    except KeyboardInterrupt:
        print("\n[تم الإيقاف بواسطة المستخدم]")
        return []


def _save(places: list[Place], args, name_hint: str) -> None:
    json_path = Path(args.output) if args.output else _default_output(name_hint)
    save_json(places, json_path)
    print(f"\n✓ تم حفظ {len(places)} سجل في:\n  {json_path}")
    if args.csv:
        save_csv(places, Path(args.csv))
        print(f"✓ تم حفظ نسخة CSV في: {args.csv}")


# --------------------------------------------------------------------------- #
def cmd_search(args) -> None:
    def fn(scraper: GoogleMapsScraper):
        return scraper.search(
            keyword=args.keyword, city=args.city, max_results=args.max
        )

    places = _run_scraper(args, fn)
    _save(places, args, name_hint=f"search_{args.keyword}_{args.city}")


def cmd_url(args) -> None:
    def fn(scraper: GoogleMapsScraper):
        try:
            return [scraper.extract_url(args.url)]
        except Exception as e:
            logging.error("فشل استخراج الرابط %s: %s", args.url, e)
            return [Place(place_url=args.url, timestamp=datetime.now().isoformat())]

    places = _run_scraper(args, fn)
    _save(places, args, name_hint="url")


def cmd_file(args) -> None:
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"❌ ملف الإدخال غير موجود: {input_path}", file=sys.stderr)
        sys.exit(1)
    # تجاهل الأسطر الفارغة والتعليقات (تبدأ بـ #)
    lines = [
        ln.strip()
        for ln in input_path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    print(f"عدد العناصر في الملف: {len(lines)}")
    if not lines:
        print("⚠️ لا توجد عناصر صالحة في الملف (كلها فارغة أو تعليقات).")

    def fn(scraper: GoogleMapsScraper):
        results: list[Place] = []
        # extract_url يتعامل مع الروابط والأسماء النصية على حدٍ سواء
        for i, line in enumerate(lines, 1):
            print(f"[{i}/{len(lines)}] {line}")
            try:
                results.append(scraper.extract_url(line))
            except Exception as e:
                print(f"   ⚠️ فشل ({type(e).__name__}): {e}")
            if args.max and len(results) >= args.max:
                break
        return results

    places = _run_scraper(args, fn)
    _save(places, args, name_hint=f"file_{input_path.stem}")


# --------------------------------------------------------------------------- #
def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """خيارات عامة. default=SUPPRESS حتى لا تُصفّر قيمةَ ما قبل الأمر الفرعي
    عند تكرارها في الأمر الفرعي (مشكلة argparse المعروفة مع parents/subparsers)."""
    parser.add_argument("--headless", dest="headless", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        default=argparse.SUPPRESS,
                        help="افتح المتصفح بشكل مرئي لتتبع العملية")
    parser.add_argument("--lang", default=argparse.SUPPRESS, help="لغة الواجهة (ar/en/...)")
    parser.add_argument("--output", default=argparse.SUPPRESS, help="مسار ملف JSON الناتج")
    parser.add_argument("--csv", default=argparse.SUPPRESS, help="حفظ نسخة CSV أيضاً")
    parser.add_argument("--proxy", default=argparse.SUPPRESS,
                        help='بروكسي: "server=host:port;username=u;password=p"')
    parser.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="سجلات تفصيلية")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="أداة سحب بيانات من Google Maps",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # القيم الافتراضية الفعلية على المُحلّل الرئيسي (تُطبَّق مرة واحدة)
    p.set_defaults(headless=True, lang="ar", output=None, csv=None,
                   proxy=None, verbose=False)
    _add_common_args(p)  # تُقبل الخيارات العامة قبل الأمر الفرعي

    sub = p.add_subparsers(dest="command", required=True)

    # search
    s = sub.add_parser("search", help="بحث بكلمة ومدينة")
    _add_common_args(s)  # ... وبعده أيضاً
    s.add_argument("--keyword", "-k", required=True, help="كلمة البحث (مطاعم، صيدليات...)")
    s.add_argument("--city", "-c", required=True, help="المدينة")
    s.add_argument("--max", "-n", type=int, default=20, help="أقصى عدد نتائج (افتراضي 20)")
    s.set_defaults(func=cmd_search)

    # url
    u = sub.add_parser("url", help="استخراج موقع واحد من رابط")
    _add_common_args(u)
    u.add_argument("--url", required=True, help="رابط Google Maps للموقع")
    u.set_defaults(func=cmd_url)

    # file
    f = sub.add_parser("file", help="استخراج قائمة من ملف نصي")
    _add_common_args(f)
    f.add_argument("--input", "-i", required=True, help="ملف نصي: سطر لكل رابط/اسم")
    f.add_argument("--max", "-n", type=int, default=0, help="أقصى عدد نتائج (0=الكل)")
    f.set_defaults(func=cmd_file)

    return p


def main() -> None:
    args = build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 60)
    print(" أداة سحب بيانات Google Maps")
    print("=" * 60)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n[تم الإيقاف]")


if __name__ == "__main__":
    main()
