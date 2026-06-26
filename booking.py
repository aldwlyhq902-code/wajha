"""
مولّد صفحات الحجز الديمو (Booking Page Generator)
=================================================
يُولّد لكل منشأة من بيانات السحب صفحة هبوط جميلة فيها:
  • واجهة بصورة المنشأة + الاسم + التقييم + الفئة.
  • معلومات: العنوان، خريطة حيّة (Google Maps embed)، ساعات العمل، الهاتف.
  • نموذج حجز فعّال يُرسل الطلب عبر واتساب مباشرةً لرقم المنشأة (بلا خادم).
  • نوع الحجز يتكيّف مع الفئة: مطعم=طاولة، عيادة/صالون=موعد، فندق=غرفة.

الفائدة: صفحة ديمو حقيقية تعمل، تُريها للعميل لإقناعه بشراء الخدمة.

التشغيل:
    python booking.py                      # كل ملفات output/*.json
    python booking.py --input output/x.json --max 30
    python booking.py --country 966
يُنتج:  output/booking/<اسم>.html  لكل منشأة  +  output/booking/index.html
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import datetime
from pathlib import Path

# إعادة استخدام أدوات الهاتف والتحميل من محرّك العملاء
from leads import normalize_phone, whatsappable, load_records

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
def big_image(url: str) -> str:
    """كبّر صورة googleusercontent المصغّرة (=w32-h32...) إلى حجم واجهة."""
    if not url:
        return ""
    return re.sub(r"=w\d+-h\d+[^&]*$", "=w1280-h500-p-k-no", url)


def stars_html(rating) -> str:
    if not rating:
        return ""
    full = int(rating)
    half = 1 if (rating - full) >= 0.5 else 0
    empty = 5 - full - half
    return "★" * full + ("⯨" * half) + "☆" * empty


def slugify(name: str, idx: int) -> str:
    s = re.sub(r"[^\w؀-ۿ]+", "-", name or "").strip("-")[:40]
    return f"{idx:03d}-{s or 'place'}"


def booking_config(category: str) -> dict:
    c = (category or "").lower()
    food = ("مطعم", "مقهى", "كافيه", "كوفي", "قهوة", "مأكولات", "برجر", "بيتزا",
            "cafe", "restaurant", "coffee")
    clinic = ("عياد", "أسنان", "طبيب", "مستشفى", "طب", "مختبر", "clinic", "dentist", "medical")
    salon = ("صالون", "حلاق", "تجميل", "سبا", "مساج", "salon", "spa", "barber", "beauty")
    hotel = ("فندق", "نزل", "شقق", "منتجع", "استراحة", "hotel", "resort", "suites")
    if any(k in c for k in food):
        return {"title": "احجز طاولتك", "cta": "احجز الآن", "count": True,
                "count_label": "عدد الأشخاص", "service": False, "service_label": ""}
    if any(k in c for k in clinic):
        return {"title": "احجز موعدك", "cta": "احجز الموعد", "count": False,
                "count_label": "", "service": True, "service_label": "سبب الزيارة / الخدمة"}
    if any(k in c for k in salon):
        return {"title": "احجز موعدك", "cta": "احجز الموعد", "count": False,
                "count_label": "", "service": True, "service_label": "الخدمة المطلوبة"}
    if any(k in c for k in hotel):
        return {"title": "احجز إقامتك", "cta": "احجز الغرفة", "count": True,
                "count_label": "عدد الضيوف", "service": False, "service_label": ""}
    return {"title": "اطلب حجزاً", "cta": "أرسل الطلب", "count": True,
            "count_label": "عدد الأشخاص", "service": False, "service_label": ""}


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


# --------------------------------------------------------------------------- #
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>%%NAME%% — حجز إلكتروني</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:"Segoe UI",Tahoma,"Cairo",sans-serif}
body{background:#eef1f4;color:#1f2933;line-height:1.6}
.wrap{max-width:880px;margin:0 auto;background:#fff;min-height:100vh;box-shadow:0 0 40px rgba(0,0,0,.06)}
.hero{position:relative;height:280px;background:%%HEROBG%%;background-size:cover;background-position:center;color:#fff}
.hero::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.15),rgba(0,0,0,.75))}
.hero .h-in{position:absolute;bottom:0;right:0;left:0;padding:26px;z-index:2}
.demo-badge{position:absolute;top:16px;left:16px;z-index:3;background:#fbbc04;color:#202124;
  font-size:12px;font-weight:700;padding:5px 12px;border-radius:20px}
.cat{font-size:14px;opacity:.9}
.hero h1{font-size:30px;font-weight:700;margin:4px 0}
.rate{font-size:15px}.rate .s{color:#fdd835;letter-spacing:2px}
.body{padding:26px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}
@media(max-width:760px){.grid{grid-template-columns:1fr}.hero{height:220px}}
.card{border:1px solid #e3e8ee;border-radius:14px;padding:18px;margin-bottom:18px}
.card h3{font-size:16px;margin-bottom:12px;color:#3b4a5a}
.row{display:flex;gap:8px;margin-bottom:7px;font-size:14px}
.row .k{color:#7b8794;min-width:70px}
.hours{list-style:none;font-size:14px}
.hours li{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dashed #eef1f4}
.map{width:100%;height:220px;border:0;border-radius:12px}
.btns{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}
.btn{flex:1;text-align:center;text-decoration:none;padding:11px;border-radius:10px;font-weight:600;font-size:14px;border:0;cursor:pointer}
.btn.call{background:#e8f0fe;color:#1a73e8}.btn.map{background:#e6f4ea;color:#137333}
.form label{display:block;font-size:13px;font-weight:600;color:#5b6b7b;margin:10px 0 5px}
.form input,.form select,.form textarea{width:100%;padding:11px;border:1px solid #d4dbe2;border-radius:10px;font-size:15px;font-family:inherit}
.form input:focus,.form select:focus,.form textarea:focus{outline:0;border-color:#1a73e8;box-shadow:0 0 0 3px rgba(26,115,232,.13)}
.f2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.book{width:100%;margin-top:16px;background:#25d366;color:#fff;border:0;padding:14px;border-radius:12px;font-size:16px;font-weight:700;cursor:pointer}
.book:hover{background:#1faa52}
.foot{padding:18px 26px;text-align:center;color:#9aa5b1;font-size:12px;border-top:1px solid #eef1f4}
</style></head><body>
<div class="wrap">
  <div class="hero">
    <span class="demo-badge">نموذج تجريبي</span>
    <div class="h-in">
      <div class="cat">%%CATEGORY%%</div>
      <h1>%%NAME%%</h1>
      <div class="rate"><span class="s">%%STARS%%</span> %%RATING%% %%REVIEWS%%</div>
    </div>
  </div>
  <div class="body">
    <div class="grid">
      <div>
        <div class="card">
          <h3>📍 الموقع</h3>
          <div class="row"><span class="k">العنوان</span><span>%%ADDRESS%%</span></div>
          <div class="row"><span class="k">الهاتف</span><span>%%PHONE%%</span></div>
          %%MAP%%
          <div class="btns">
            %%CALLBTN%%
            %%MAPBTN%%
          </div>
        </div>
        <div class="card">
          <h3>🕒 ساعات العمل</h3>
          <ul class="hours">%%HOURS%%</ul>
        </div>
      </div>
      <div>
        <div class="card form">
          <h3>%%BTITLE%%</h3>
          <label>الاسم</label><input id="b_name" placeholder="اسمك الكريم">
          <label>رقم الجوال</label><input id="b_phone" inputmode="tel" placeholder="05xxxxxxxx">
          <div class="f2">
            <div><label>التاريخ</label><input id="b_date" type="date"></div>
            <div><label>الوقت</label><input id="b_time" type="time"></div>
          </div>
          %%COUNTFIELD%%
          %%SERVICEFIELD%%
          <label>ملاحظات (اختياري)</label><textarea id="b_notes" rows="2" placeholder="أي تفاصيل إضافية"></textarea>
          <button class="book" onclick="book()">%%CTA%% عبر واتساب</button>
        </div>
      </div>
    </div>
  </div>
  <div class="foot">صفحة حجز تجريبية — تُرسل الطلبات مباشرةً لواتساب المنشأة. تصميم جاهز للتفعيل.</div>
</div>
<script>
function book(){
  var g=function(id){var e=document.getElementById(id);return e?e.value.trim():'';};
  var name=g('b_name'),phone=g('b_phone'),date=g('b_date'),time=g('b_time'),
      count=g('b_count'),service=g('b_service'),notes=g('b_notes');
  if(!name||!phone||!date){alert('فضلاً أدخل الاسم ورقم الجوال والتاريخ');return;}
  var lines=['🗓️ طلب حجز جديد عبر صفحة الحجز','المنشأة: %%NAMEJS%%','الاسم: '+name,'الجوال: '+phone,'التاريخ: '+date];
  if(time)lines.push('الوقت: '+time);
  if(count)lines.push('%%COUNTLABEL%%: '+count);
  if(service)lines.push('%%SERVICELABEL%%: '+service);
  if(notes)lines.push('ملاحظات: '+notes);
  var msg=lines.join('\\n');
  var num='%%WANUM%%';
  if(num){window.open('https://wa.me/'+num+'?text='+encodeURIComponent(msg),'_blank');}
  else{alert('عيّن رقم واتساب المنشأة لتفعيل الحجز.');}
}
</script>
</body></html>"""


def render_page(r: dict, country: str) -> str:
    cfg = booking_config(r.get("category", ""))
    intl = normalize_phone(r.get("phone"), country)
    wa = intl if (intl and whatsappable(intl, r.get("phone"), country)) else (intl or "")

    img = big_image(r.get("image_url", ""))
    herobg = (f"url('{_e(img)}'),linear-gradient(135deg,#4285f4,#34a853)"
              if img else "linear-gradient(135deg,#4285f4,#34a853)")

    lat, lng = r.get("latitude"), r.get("longitude")
    if lat and lng:
        map_embed = (f'<iframe class="map" loading="lazy" '
                     f'src="https://maps.google.com/maps?q={lat},{lng}&z=16&output=embed"></iframe>')
        map_btn = (f'<a class="btn map" target="_blank" '
                   f'href="https://www.google.com/maps/@{lat},{lng},17z">🗺️ الخريطة</a>')
    else:
        map_embed, map_btn = "", ""

    call_btn = (f'<a class="btn call" href="tel:+{intl}">📞 اتصال</a>' if intl else "")

    hours = r.get("opening_hours") or []
    if hours:
        items = []
        for h in hours:
            parts = h.split(" ", 1)
            day = parts[0]
            tm = parts[1] if len(parts) > 1 else ""
            items.append(f"<li><span>{_e(day)}</span><span>{_e(tm)}</span></li>")
        hours_html = "".join(items)
    else:
        hours_html = "<li><span>غير متوفّرة</span><span></span></li>"

    reviews = (f"({r.get('reviews_count')} مراجعة)" if r.get("reviews_count") else "")

    count_field = (
        f'<label>{_e(cfg["count_label"])}</label>'
        f'<select id="b_count"><option>1</option><option>2</option><option>3</option>'
        f'<option>4</option><option>5</option><option>6</option><option>أكثر من 6</option></select>'
        if cfg["count"] else ""
    )
    service_field = (
        f'<label>{_e(cfg["service_label"])}</label>'
        f'<input id="b_service" placeholder="{_e(cfg["service_label"])}">'
        if cfg["service"] else ""
    )

    repl = {
        "%%NAME%%": _e(r.get("name", "")),
        "%%NAMEJS%%": (r.get("name", "") or "").replace("'", "\\'").replace("\\", ""),
        "%%CATEGORY%%": _e(r.get("category", "")),
        "%%STARS%%": stars_html(r.get("rating")),
        "%%RATING%%": _e(r.get("rating")) if r.get("rating") else "",
        "%%REVIEWS%%": _e(reviews),
        "%%HEROBG%%": herobg,
        "%%ADDRESS%%": _e(r.get("address", "")) or "—",
        "%%PHONE%%": _e(r.get("phone", "")) or "—",
        "%%MAP%%": map_embed,
        "%%CALLBTN%%": call_btn,
        "%%MAPBTN%%": map_btn,
        "%%HOURS%%": hours_html,
        "%%BTITLE%%": _e(cfg["title"]),
        "%%CTA%%": _e(cfg["cta"]),
        "%%COUNTFIELD%%": count_field,
        "%%SERVICEFIELD%%": service_field,
        "%%COUNTLABEL%%": _e(cfg["count_label"]) or "العدد",
        "%%SERVICELABEL%%": _e(cfg["service_label"]) or "الخدمة",
        "%%WANUM%%": wa,
    }
    out = PAGE_TEMPLATE
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def render_index(items: list[dict]) -> str:
    cards = []
    for it in items:
        cards.append(f"""<a class="c" href="{_e(it['file'])}">
  <div class="t">{_e(it['name'])}</div>
  <div class="m">{_e(it['category'])} · {_e(it['rating']) if it['rating'] else '—'}⭐</div>
  <div class="w">{'📲 حجز واتساب مفعّل' if it['wa'] else '📞 اتصال'}</div>
</a>""")
    return f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>صفحات الحجز</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:"Segoe UI",Tahoma,sans-serif}}
body{{background:#eef1f4;color:#1f2933;padding:26px}}
h1{{font-size:24px;margin-bottom:4px}}.muted{{color:#7b8794;margin-bottom:20px}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}}
.c{{display:block;background:#fff;border:1px solid #e3e8ee;border-radius:14px;padding:16px;text-decoration:none;color:inherit;transition:.15s}}
.c:hover{{box-shadow:0 6px 20px rgba(0,0,0,.08);transform:translateY(-2px)}}
.t{{font-weight:700;font-size:15px;margin-bottom:4px}}.m{{font-size:13px;color:#7b8794}}
.w{{margin-top:8px;font-size:12px;color:#137333}}
</style></head><body>
<h1>📑 صفحات الحجز الجاهزة</h1>
<p class="muted">{len(items)} صفحة. اضغط أي منشأة لفتح صفحة حجزها الديمو.</p>
<div class="g">{''.join(cards)}</div>
</body></html>"""


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="مولّد صفحات الحجز الديمو من بيانات Google Maps")
    ap.add_argument("--input", "-i", nargs="*", default=["output/*.json"],
                    help="ملف/ملفات JSON (يدعم *). الافتراضي: output/*.json")
    ap.add_argument("--country", "-c", default="966", help="رمز الدولة للهاتف (افتراضي 966)")
    ap.add_argument("--max", "-n", type=int, default=50, help="أقصى عدد صفحات (افتراضي 50)")
    ap.add_argument("--outdir", "-o", default="output/booking", help="مجلد الإخراج")
    args = ap.parse_args()

    records = load_records(args.input)
    if not records:
        print("❌ لا توجد سجلات. شغّل السحب أولاً ثم أعد المحاولة.")
        sys.exit(1)
    records = records[:args.max]

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    index_items = []
    for i, r in enumerate(records, 1):
        page = render_page(r, args.country)
        fname = slugify(r.get("name", ""), i) + ".html"
        (outdir / fname).write_text(page, encoding="utf-8")
        intl = normalize_phone(r.get("phone"), args.country)
        index_items.append({
            "name": r.get("name", ""), "category": r.get("category", ""),
            "rating": r.get("rating"), "file": fname,
            "wa": bool(intl and whatsappable(intl, r.get("phone"), args.country)),
        })

    (outdir / "index.html").write_text(render_index(index_items), encoding="utf-8")

    print("=" * 60)
    print(" 📑 مولّد صفحات الحجز الديمو")
    print("=" * 60)
    print(f"تم توليد {len(index_items)} صفحة في: {outdir}")
    print(f"افتح الفهرس: {outdir / 'index.html'}")
    n_wa = sum(1 for it in index_items if it["wa"])
    print(f"منها {n_wa} صفحة بحجز واتساب مفعّل (أرقام جوّالة).")


if __name__ == "__main__":
    main()
