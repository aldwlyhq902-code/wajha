"""
مولّد صفحة هبوط خاصة لكل نشاط (منشأة) — بهوية «واجهة» + حجز عبر واتساب (whats_bot).
=================================================================================
لكل منشأة في بيانات السحب: صفحة أنيقة مستقلة (اسم، تقييم، خدمات، ساعات، خريطة)
وزر «احجز عبر واتساب» يفتح محادثة مع رقم المنشأة برسالة حجز — يلتقطها بوت whats_bot.

التشغيل:
    python bizpage.py                       # كل output/*.json
    python bizpage.py --input output/salons.json --country 966 --brand واجهة
يُنتج:  landing/sites/<id>/index.html  لكل منشأة  +  index.html  +  روابط CSV

للنشر (كل منشأة يصلها رابطها):
    vercel deploy landing/sites --prod --yes --scope <نطاقك>
    أو:  python publish.py ... (نفس آلية النشر)
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

from leads import load_records, normalize_phone, whatsappable
from booking import big_image, stars_html, booking_config

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def feature_id(url: str) -> str:
    m = re.search(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", url or "")
    return m.group(0) if m else ""


def publish_id(r: dict) -> str:
    import hashlib
    key = feature_id(r.get("place_url", "")) or (r.get("name", "") + (r.get("phone", "") or ""))
    return "g" + hashlib.md5(key.encode("utf-8")).hexdigest()[:8]


def _services(category: str) -> list[str]:
    c = (category or "").lower()
    if any(k in c for k in ("صالون", "تجميل", "حلاق", "سبا", "spa", "salon", "barber")):
        return ["قص وتصفيف", "صبغة وعلاج", "عناية وبشرة", "مكياج ومناسبات"]
    if any(k in c for k in ("عياد", "أسنان", "طبيب", "clinic", "dental")):
        return ["كشف ومتابعة", "تنظيف وتبييض", "علاج وحشوات", "استشارة"]
    if any(k in c for k in ("مطعم", "مقهى", "كافيه", "cafe", "restaurant", "coffee")):
        return ["حجز طاولة", "مناسبات وعزائم", "طلبات خاصة", "قاعة عائلية"]
    if any(k in c for k in ("فندق", "نزل", "شقق", "hotel", "resort")):
        return ["حجز غرف", "أجنحة عائلية", "إقامة طويلة", "خدمات الضيافة"]
    return ["حجز موعد", "استشارة", "خدمة مميّزة", "متابعة"]


PAGE = """<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8"><meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>%%NAME%% — احجز موعدك عبر واتساب</title>
<style>
:root{--brand:#128C7E;--brand-d:#0c6b60;--green:#25D366;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--bg2:#f1f5f9}
*{box-sizing:border-box;margin:0;padding:0;font-family:"Segoe UI",Tahoma,"Cairo",sans-serif}
body{color:var(--ink);background:#fff;line-height:1.7}
a{text-decoration:none;color:inherit}
.wrap{max-width:760px;margin:0 auto;padding:0 18px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font-weight:700;font-size:15px;
  padding:13px 22px;border-radius:12px;border:0;cursor:pointer;transition:.18s}
.btn.wa{background:var(--green);color:#04361f}.btn.wa:hover{background:#1fb858}
.btn.g{background:#eef2f6;color:#334155}.btn.o{background:#fff;color:var(--brand);border:1.5px solid var(--line)}
/* top bar */
.bar{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.92);backdrop-filter:blur(8px);
  border-bottom:1px solid var(--line)}
.bar-in{max-width:760px;margin:0 auto;padding:10px 18px;display:flex;justify-content:space-between;align-items:center}
.bar .nm{font-weight:800;font-size:16px}
/* hero */
.hero{position:relative;min-height:340px;display:flex;align-items:flex-end;color:#fff;
  background:%%HEROBG%%;background-size:cover;background-position:center}
.hero::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(8,30,27,.25),rgba(8,30,27,.85))}
.hero .h-in{position:relative;z-index:2;padding:26px 18px;max-width:760px;margin:0 auto;width:100%}
.hero .cat{opacity:.9;font-size:14px}
.hero h1{font-size:30px;font-weight:850;margin:4px 0}
.hero .meta{font-size:14px;opacity:.95}.hero .meta .s{color:#fdd835;letter-spacing:1px}
.cta-row{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
/* sections */
section{padding:30px 0;border-bottom:1px solid var(--bg2)}
h2{font-size:20px;margin-bottom:14px}
.chips{display:flex;gap:10px;flex-wrap:wrap}
.chip{background:#e7f7f0;color:var(--brand-d);font-weight:700;font-size:14px;padding:9px 15px;border-radius:20px}
.hours{list-style:none;font-size:15px}
.hours li{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px dashed var(--bg2)}
.map{width:100%;height:240px;border:0;border-radius:14px}
.info{display:flex;gap:8px;color:var(--muted);font-size:15px;margin-bottom:6px}
/* booking band */
.book{background:linear-gradient(135deg,var(--brand-d),var(--brand));color:#fff;border-radius:20px;
  padding:34px 22px;text-align:center;margin:24px 0}
.book h2{color:#fff;font-size:24px}.book p{opacity:.92;margin:8px 0 18px}
.book .btn.wa{font-size:17px;padding:15px 30px}
/* footer */
footer{padding:26px 18px;text-align:center;color:var(--muted);font-size:13px}
footer a{color:var(--brand);font-weight:700}
</style></head><body>

<div class="bar"><div class="bar-in"><span class="nm">%%NAME%%</span>
  <a class="btn wa" style="padding:9px 16px" href="%%WA%%" target="_blank">احجز عبر واتساب</a></div></div>

<div class="hero"><div class="h-in">
  <div class="cat">%%CATEGORY%%</div>
  <h1>%%NAME%%</h1>
  <div class="meta"><span class="s">%%STARS%%</span> %%RATING%% %%REVIEWS%%</div>
  <div class="cta-row">
    <a class="btn wa" href="%%WA%%" target="_blank">💬 احجز عبر واتساب</a>
    %%CALL%%
    %%MAPBTN%%
  </div>
</div></div>

<div class="wrap">
  <section><h2>خدماتنا</h2><div class="chips">%%CHIPS%%</div></section>

  <section><h2>📍 الموقع</h2>
    <div class="info">%%ADDRESS%%</div>
    %%MAP%%
  </section>

  <section><h2>🕒 ساعات العمل</h2><ul class="hours">%%HOURS%%</ul></section>

  <div class="book">
    <h2>احجز موعدك الآن</h2>
    <p>أرسل لنا على واتساب وسيردّ عليك مساعدنا الذكي فوراً ويحجز لك 🗓️</p>
    <a class="btn wa" href="%%WA%%" target="_blank">💬 ابدأ الحجز على واتساب</a>
  </div>
</div>

<footer>هذه الصفحة مقدّمة بـ <a href="#">واجهة</a> — واتساب ذكي وحجز إلكتروني للمنشآت.</footer>
%%PIXEL%%
</body></html>"""

# الرابط الافتراضي للوحة السحابية (لبكسل رصد فتح الصفحة)
CLOUD_DEFAULT = "https://booking-system-y5id.onrender.com"


def render_business_page(r: dict, country: str = "966", brand: str = "واجهة",
                         track_url: str = "") -> str:
    name = r.get("name", "")
    intl = normalize_phone(r.get("phone"), country)
    can_wa = bool(intl and whatsappable(intl, r.get("phone"), country))
    noun = booking_config(r.get("category", "")).get("title", "احجز الآن")
    msg = f"السلام عليكم، أرغب بحجز موعد في {name} 🗓️"
    wa = f"https://wa.me/{intl}?text={quote(msg)}" if can_wa else (f"tel:+{intl}" if intl else "#")

    img = big_image(r.get("image_url", ""))
    herobg = (f"url('{_e(img)}'),linear-gradient(135deg,#0c6b60,#128C7E)"
              if img else "linear-gradient(135deg,#0c6b60,#128C7E)")

    lat, lng = r.get("latitude"), r.get("longitude")
    if lat and lng:
        mapembed = (f'<iframe class="map" loading="lazy" '
                    f'src="https://maps.google.com/maps?q={lat},{lng}&z=16&output=embed"></iframe>')
        mapbtn = (f'<a class="btn o" target="_blank" '
                  f'href="https://www.google.com/maps/@{lat},{lng},17z">🗺️ الخريطة</a>')
    else:
        mapembed, mapbtn = "", ""

    call = f'<a class="btn g" href="tel:+{intl}">📞 اتصال</a>' if intl else ""
    chips = "".join(f'<span class="chip">{_e(s)}</span>' for s in _services(r.get("category", "")))

    hours = r.get("opening_hours") or []
    if hours:
        items = []
        for h in hours:
            parts = h.split(" ", 1)
            items.append(f"<li><span>{_e(parts[0])}</span><span>{_e(parts[1] if len(parts) > 1 else '')}</span></li>")
        hours_html = "".join(items)
    else:
        hours_html = "<li><span>اتصل لمعرفة الأوقات</span><span></span></li>"

    reviews = f"({r.get('reviews_count')} تقييم)" if r.get("reviews_count") else ""
    fid = feature_id(r.get("place_url", ""))
    pixel = (f'<img src="{_e(track_url.rstrip("/"))}/api/track/open?fid={_e(fid)}" '
             f'width="1" height="1" alt="" style="position:absolute;left:-9999px;top:auto">'
             if (track_url and fid) else "")
    repl = {
        "%%NAME%%": _e(name), "%%CATEGORY%%": _e(r.get("category", "")),
        "%%STARS%%": stars_html(r.get("rating")), "%%RATING%%": _e(r.get("rating")) if r.get("rating") else "",
        "%%REVIEWS%%": _e(reviews), "%%HEROBG%%": herobg, "%%WA%%": _e(wa),
        "%%CALL%%": call, "%%MAPBTN%%": mapbtn, "%%MAP%%": mapembed,
        "%%ADDRESS%%": _e(r.get("address", "")) or "—",
        "%%CHIPS%%": chips, "%%HOURS%%": hours_html, "%%PIXEL%%": pixel,
    }
    out = PAGE
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def build(records, outdir="landing/sites", country="966", brand="واجهة",
          base_url="", track_url="") -> list[dict]:
    out = Path(outdir)
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")

    rows, seen = [], set()
    for r in records:
        pid = publish_id(r)
        if pid in seen:
            continue
        seen.add(pid)
        (out / pid).mkdir(parents=True, exist_ok=True)
        (out / pid / "index.html").write_text(
            render_business_page(r, country, brand, track_url), encoding="utf-8")
        url = (base_url.rstrip("/") + "/" + pid + "/") if base_url else ""
        rows.append({"name": r.get("name", ""), "phone": r.get("phone", ""),
                     "feature_id": feature_id(r.get("place_url", "")), "pid": pid, "url": url})

    # جذر محايد (لا يكشف قائمة المنشآت) + إعداد Vercel — جاهز للنشر
    (out / "index.html").write_text(
        '<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">'
        '<meta name="robots" content="noindex,nofollow">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>صفحات الحجز</title><style>body{font-family:"Segoe UI",Tahoma,sans-serif;'
        'background:#f1f5f9;color:#64748b;display:flex;min-height:100vh;align-items:center;'
        'justify-content:center;margin:0;text-align:center}</style></head>'
        '<body><p>🗓️ صفحات الحجز — افتح رابط منشأتك المخصّص.</p></body></html>',
        encoding="utf-8")
    (out / "vercel.json").write_text(
        '{\n  "cleanUrls": true,\n  "headers": [\n    {\n      "source": "/(.*)",\n'
        '      "headers": [ { "key": "X-Robots-Tag", "value": "noindex, nofollow" } ]\n'
        '    }\n  ]\n}\n', encoding="utf-8")

    lp = Path("output") / "bizpages_links.csv"
    lp.parent.mkdir(parents=True, exist_ok=True)
    with lp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "phone", "feature_id", "pid", "url"])
        w.writeheader()
        w.writerows(rows)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="مولّد صفحة هبوط لكل منشأة (واجهة + حجز واتساب)")
    ap.add_argument("--input", "-i", nargs="*", default=["output/*.json"])
    ap.add_argument("--country", "-c", default="966")
    ap.add_argument("--brand", default="واجهة")
    ap.add_argument("--outdir", "-o", default="landing/sites")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--track-url", default=CLOUD_DEFAULT,
                    help="رابط اللوحة لبكسل رصد فتح الصفحة (فارغ=تعطيل)")
    args = ap.parse_args()

    records = load_records(args.input)
    if not records:
        print("❌ لا توجد بيانات. اسحب أولاً (run.py / pipeline.py).")
        sys.exit(1)
    rows = build(records, args.outdir, args.country, args.brand, args.base_url, args.track_url)
    print("=" * 56)
    print(f" 🌐 تم توليد {len(rows)} صفحة منشأة في: {args.outdir}")
    print(f" روابط: output/bizpages_links.csv")
    if args.base_url:
        for x in rows[:5]:
            print(f"  {x['name'][:28]:<28} → {x['url']}")
    print("\nللنشر: vercel deploy " + args.outdir + " --prod --yes --scope <نطاقك>")


if __name__ == "__main__":
    main()
