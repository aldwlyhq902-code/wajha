"""
مولّد حملة التواصل (Outreach Kit Generator)
============================================
لكل منشأة من بيانات السحب، يُنتج حزمة تواصل كاملة جاهزة للإرسال:

  outreach/<اسم>/landing.html   ← صفحة الحجز/الهبوط الجاهزة (تعمل بواتساب)
  outreach/<اسم>/proposal.html  ← مقترح احترافي: تحليل + المشكلة + الحل + الفائدة بالأرقام
  outreach/<اسم>/message.txt    ← الرسالة التسويقية الجاهزة
  outreach/index.html           ← لوحة الحملة: لكل منشأة (نسخ الرسالة / واتساب / المقترح)

المنطق: يحلّل ما ينقص كل منشأة (موقع؟ حجز؟ حضور رقمي؟) ويقترح الحزمة المناسبة،
ثم يحسب الفائدة المتوقّعة بأرقام شفّافة (افتراضات قابلة للتعديل).

التشغيل:
    python outreach.py                         # كل output/*.json
    python outreach.py --input output/salons.json --country 966
    python outreach.py --ticket 120 --weekly-extra 10   # افتراضات العائد
"""

from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# إعادة استخدام ما بُني سابقاً
from leads import (normalize_phone, whatsappable, load_records,
                   score_lead, category_weight)
from booking import render_page as render_landing, booking_config, slugify

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _noun(category: str) -> str:
    """كلمة الحجز المناسبة للفئة: موعد/طاولة/غرفة/حجز."""
    cfg = booking_config(category)
    t = cfg["title"]
    if "طاولة" in t:
        return "طاولة"
    if "موعد" in t:
        return "موعد"
    if "غرفة" in t:
        return "غرفة"
    return "حجز"


# --------------------------------------------------------------------------- #
# التحليل + التوصية + الفائدة                                                 #
# --------------------------------------------------------------------------- #
def analyze(r: dict) -> dict:
    needs_website = not (r.get("website") or "").strip()
    has_hours = bool(r.get("opening_hours"))
    rating = r.get("rating")
    reviews = r.get("reviews_count") or 0

    gaps = []
    if needs_website:
        gaps.append("لا يملك موقعاً/صفحة هبوط احترافية")
    gaps.append("لا يوفّر حجزاً إلكترونياً (الحجز عبر الهاتف فقط)")
    if not has_hours:
        gaps.append("ساعات العمل غير ظاهرة بوضوح أونلاين")
    if reviews and rating and rating >= 4.3:
        strength = f"سمعة قوية ({rating}⭐، {reviews} مراجعة) غير مستثمَرة رقمياً"
    else:
        strength = "حضور رقمي محدود مقارنة بالمنافسين"

    return {
        "needs_website": needs_website,
        "has_hours": has_hours,
        "gaps": gaps,
        "strength": strength,
        "rating": rating,
        "reviews": reviews,
    }


def recommendation(a: dict) -> str:
    if a["needs_website"]:
        return "حزمة متكاملة: صفحة هبوط احترافية + نظام حجز إلكتروني ٢٤/٧"
    return "نظام حجز إلكتروني ٢٤/٧ + تحسين صفحة الحضور الرقمي"


def estimate_benefit(r: dict, ticket: float, weekly_extra: int) -> dict:
    """فائدة تقديرية شفّافة (الافتراضات ظاهرة وقابلة للتعديل)."""
    monthly_extra = round(ticket * weekly_extra * 4)
    yearly_extra = monthly_extra * 12
    return {
        "after_hours_pct": 40,      # نسبة الحجوزات التي تأتي خارج الدوام (تقديري عام)
        "noshow_cut_pct": 30,       # خفض الغياب عبر التأكيد/التذكير (تقديري)
        "weekly_extra": weekly_extra,
        "ticket": round(ticket),
        "monthly_extra": monthly_extra,
        "yearly_extra": yearly_extra,
    }


def marketing_message(r: dict, noun: str) -> str:
    name = r.get("name") or "صالونكم"
    rating = r.get("rating")
    reviews = r.get("reviews_count")
    praise = ""
    if rating and reviews:
        praise = f"لاحظنا تقييمكم المميّز ({rating}⭐ و{reviews} مراجعة)، "
    return (
        f"مرحباً {name} 👋\n"
        f"{praise}ويسعدنا خدمتكم.\n"
        f"جهّزنا لكم *صفحة حجز إلكتروني* جاهزة تتيح لعملائكم حجز ال{noun} على مدار الساعة، "
        f"تقلّل الغياب وتخفّف ضغط المكالمات — إضافةً لصفحة تعريفية احترافية.\n"
        f"نودّ عرضها عليكم خلال دقيقتين فقط. متى يناسبكم؟"
    )


# --------------------------------------------------------------------------- #
# مقترح العميل (HTML)                                                          #
# --------------------------------------------------------------------------- #
PROPOSAL_TMPL = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>عرض رقمي — %%NAME%%</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:"Segoe UI",Tahoma,"Cairo",sans-serif}
body{background:#eef1f4;color:#1f2933;line-height:1.7}
.wrap{max-width:820px;margin:0 auto;background:#fff}
.hero{background:linear-gradient(135deg,#7b2ff7,#1a73e8);color:#fff;padding:40px 28px;text-align:center}
.hero .tag{font-size:14px;opacity:.9;letter-spacing:1px}
.hero h1{font-size:30px;margin:6px 0}
.hero .r{font-size:15px;opacity:.95}
.sec{padding:26px 28px;border-bottom:1px solid #eef1f4}
.sec h2{font-size:19px;margin-bottom:14px}
.list{list-style:none}.list li{padding:7px 0;display:flex;gap:8px}
.list li::before{content:"•";color:#7b2ff7;font-weight:700}
.bad li::before{content:"✕";color:#ea4335}
.good li::before{content:"✓";color:#34a853}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-top:6px}
.kpi{background:#f7f5ff;border:1px solid #e6dcff;border-radius:14px;padding:16px;text-align:center}
.kpi b{display:block;font-size:26px;color:#7b2ff7}
.kpi.green b{color:#137333}.kpi.green{background:#e6f4ea;border-color:#bfe3c8}
.cta{display:inline-block;background:#25d366;color:#fff;text-decoration:none;padding:14px 26px;border-radius:12px;font-weight:700;font-size:16px;margin-top:6px}
.cta.b{background:#1a73e8}
.note{font-size:12px;color:#7b8794;margin-top:10px}
.foot{padding:22px 28px;text-align:center;color:#9aa5b1;font-size:13px}
</style></head><body><div class="wrap">
<div class="hero"><div class="tag">عرض تحوّل رقمي خاص</div>
  <h1>%%NAME%%</h1>
  <div class="r">%%RATELINE%%</div></div>

<div class="sec"><h2>📊 ماذا وجدنا</h2>
  <ul class="list">
    <li>التصنيف: %%CATEGORY%%</li>
    <li>%%STRENGTH%%</li>
    <li>العنوان: %%ADDRESS%%</li>
  </ul></div>

<div class="sec"><h2>⚠️ الفرص الضائعة حالياً</h2>
  <ul class="list bad">%%GAPS%%</ul></div>

<div class="sec"><h2>✅ الحل المقترح</h2>
  <p style="margin-bottom:12px">%%RECO%%</p>
  <a class="cta b" href="landing.html" target="_blank">👀 شاهد صفحة الحجز الجاهزة لمنشأتك</a>
  <div class="note">صفحة فعلية تعمل الآن ببياناتكم — الحجز يصل واتساب مباشرةً.</div></div>

<div class="sec"><h2>📈 الفائدة المتوقّعة</h2>
  <div class="cards">
    <div class="kpi"><b>%%AFTERHOURS%%٪</b>حجوزات خارج الدوام تُلتقَط بدل أن تضيع</div>
    <div class="kpi"><b>%%NOSHOW%%٪</b>خفض الغياب عبر التأكيد والتذكير</div>
    <div class="kpi green"><b>%%MONTHLY%%</b>ريال إضافية شهرياً (تقديري)</div>
    <div class="kpi green"><b>%%YEARLY%%</b>ريال سنوياً (تقديري)</div>
  </div>
  <div class="note">تقدير متحفّظ بافتراض %%WEEKLY%% حجوزات إضافية أسبوعياً بمتوسط فاتورة %%TICKET%% ريال — قابل للتعديل حسب نشاطكم.</div></div>

<div class="sec" style="text-align:center"><h2>🚀 جاهزون للانطلاق</h2>
  <p style="margin-bottom:14px">نفعّلها باسمكم خلال يوم واحد: شعاركم، خدماتكم، وأوقاتكم.</p>
  %%WABTN%%
  <div class="note">%%PHONE%%</div></div>

<div class="foot">عرض مُعَدّ خصّيصاً لـ%%NAME%% — %%DATE%%</div>
</div></body></html>"""


def render_proposal(r, a, bn, noun, country, ben) -> str:
    rating = r.get("rating")
    reviews = r.get("reviews_count")
    rateline = ""
    if rating:
        rateline = f"★ {rating}" + (f" · {reviews} مراجعة" if reviews else "")
    gaps_html = "".join(f"<li>{_e(g)}</li>" for g in a["gaps"])

    intl = normalize_phone(r.get("phone"), country)
    if intl and whatsappable(intl, r.get("phone"), country):
        wa = f"https://wa.me/{intl}?text={quote(bn)}"
        wabtn = f'<a class="cta" href="{_e(wa)}" target="_blank">📲 ابدأ عبر واتساب</a>'
    else:
        wabtn = ('<a class="cta b" href="#">📞 تواصل معنا للبدء</a>')

    repl = {
        "%%NAME%%": _e(r.get("name", "")),
        "%%RATELINE%%": _e(rateline),
        "%%CATEGORY%%": _e(r.get("category", "")) or "—",
        "%%STRENGTH%%": _e(a["strength"]),
        "%%ADDRESS%%": _e(r.get("address", "")) or "—",
        "%%GAPS%%": gaps_html,
        "%%RECO%%": _e(recommendation(a)),
        "%%AFTERHOURS%%": str(ben["after_hours_pct"]),
        "%%NOSHOW%%": str(ben["noshow_cut_pct"]),
        "%%MONTHLY%%": f"{ben['monthly_extra']:,}",
        "%%YEARLY%%": f"{ben['yearly_extra']:,}",
        "%%WEEKLY%%": str(ben["weekly_extra"]),
        "%%TICKET%%": str(ben["ticket"]),
        "%%WABTN%%": wabtn,
        "%%PHONE%%": _e(r.get("phone", "")),
        "%%DATE%%": datetime.now().strftime("%Y-%m-%d"),
    }
    out = PROPOSAL_TMPL
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


# --------------------------------------------------------------------------- #
# لوحة الحملة (index)                                                          #
# --------------------------------------------------------------------------- #
def render_index(items) -> str:
    import json as _json
    msgs = {it["slug"]: it["message"] for it in items}
    rows = []
    for it in items:
        wa = (f'<a class="b wa" href="{_e(it["wa"])}" target="_blank">واتساب</a>'
              if it["wa"] else "")
        rows.append(f"""<tr>
  <td><span class="sc">{it['score']}</span></td>
  <td><strong>{_e(it['name'])}</strong><div class="m">{_e(it['category'])}</div></td>
  <td><span class="{'tagno' if it['needs_website'] else 'tagok'}">{'بلا موقع' if it['needs_website'] else 'لديه موقع'}</span></td>
  <td class="m">{_e(it['benefit'])}</td>
  <td class="act">
    <a class="b" href="{_e(it['slug'])}/proposal.html" target="_blank">المقترح</a>
    <a class="b o" href="{_e(it['slug'])}/landing.html" target="_blank">الصفحة</a>
    <button class="b o" onclick="copyMsg('{_e(it['slug'])}')">نسخ الرسالة</button>
    {wa}
  </td>
</tr>""")
    return """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>لوحة الحملة</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:"Segoe UI",Tahoma,sans-serif}
body{background:#eef1f4;color:#1f2933;padding:24px}
h1{font-size:24px}.muted{color:#7b8794;margin-bottom:18px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e8ee;border-radius:14px;overflow:hidden}
th,td{padding:11px 12px;text-align:right;border-bottom:1px solid #eef1f4;font-size:14px;vertical-align:middle}
th{background:#f8f9fa;color:#5f6368;font-size:13px}
.m{font-size:12px;color:#7b8794}
.sc{display:inline-block;min-width:32px;text-align:center;background:#7b2ff7;color:#fff;border-radius:20px;padding:4px 8px;font-weight:700}
.tagno{background:#fce8e6;color:#c5221f;padding:3px 9px;border-radius:20px;font-size:12px}
.tagok{background:#e6f4ea;color:#137333;padding:3px 9px;border-radius:20px;font-size:12px}
.act{white-space:nowrap}
.b{display:inline-block;text-decoration:none;font-size:13px;padding:6px 11px;border-radius:8px;margin-inline-start:4px;border:0;cursor:pointer;background:#1a73e8;color:#fff}
.b.o{background:#eef1f4;color:#3b4a5a}.b.wa{background:#25d366}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#202124;color:#fff;padding:10px 18px;border-radius:10px;display:none}
</style></head><body>
<h1>🎯 لوحة حملة التواصل</h1>
<p class="muted">__N__ منشأة جاهزة. لكل واحدة: مقترح احترافي + صفحة حجز + رسالة جاهزة.</p>
<table><thead><tr><th>الأولوية</th><th>المنشأة</th><th>الموقع</th><th>الفائدة المتوقعة</th><th>إجراءات</th></tr></thead>
<tbody>__ROWS__</tbody></table>
<div class="toast" id="toast">تم نسخ الرسالة ✓</div>
<script>
const MSGS=__MSGS__;
function copyMsg(slug){
  navigator.clipboard.writeText(MSGS[slug]||'').then(()=>{
    const t=document.getElementById('toast');t.style.display='block';
    setTimeout(()=>t.style.display='none',1500);
  });
}
</script></body></html>""".replace("__N__", str(len(items))).replace("__ROWS__", "".join(rows)).replace("__MSGS__", _json.dumps(msgs, ensure_ascii=False))


# --------------------------------------------------------------------------- #
def build_kits(records, outdir="output/outreach", country="966",
               ticket=100.0, weekly_extra=8) -> list[dict]:
    """يُنتج لكل سجل: landing + proposal + message، ولوحة index. يُعيد قائمة العناصر."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []
    for i, r in enumerate(records, 1):
        a = analyze(r)
        noun = _noun(r.get("category", ""))
        ben = estimate_benefit(r, ticket, weekly_extra)
        bn = marketing_message(r, noun)

        slug = slugify(r.get("name", ""), i)
        bdir = outdir / slug
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "landing.html").write_text(render_landing(r, country), encoding="utf-8")
        (bdir / "proposal.html").write_text(render_proposal(r, a, bn, noun, country, ben), encoding="utf-8")
        (bdir / "message.txt").write_text(bn, encoding="utf-8")

        intl = normalize_phone(r.get("phone"), country)
        wa = (f"https://wa.me/{intl}?text={quote(bn)}"
              if (intl and whatsappable(intl, r.get("phone"), country)) else "")
        score, _ = score_lead(r, intl, bool(wa))
        items.append({
            "slug": slug, "name": r.get("name", ""), "category": r.get("category", ""),
            "needs_website": a["needs_website"], "score": score,
            "benefit": f"+{ben['monthly_extra']:,} ريال/شهر تقديري · {ben['after_hours_pct']}٪ حجوزات بعد الدوام",
            "message": bn, "wa": wa,
        })

    items.sort(key=lambda x: x["score"], reverse=True)
    (outdir / "index.html").write_text(render_index(items), encoding="utf-8")
    return items


def main() -> None:
    ap = argparse.ArgumentParser(description="مولّد حملة التواصل من بيانات Google Maps")
    ap.add_argument("--input", "-i", nargs="*", default=["output/*.json"])
    ap.add_argument("--country", "-c", default="966")
    ap.add_argument("--outdir", "-o", default="output/outreach")
    ap.add_argument("--ticket", type=float, default=100.0, help="متوسط الفاتورة (ريال) لتقدير العائد")
    ap.add_argument("--weekly-extra", type=int, default=8, help="حجوزات إضافية أسبوعية مقدّرة")
    ap.add_argument("--max", "-n", type=int, default=50)
    args = ap.parse_args()

    records = load_records(args.input)
    if not records:
        print("❌ لا توجد سجلات. اسحب أولاً (مثال: python run.py search -k صالونات -c الرياض -n 8)")
        sys.exit(1)
    records = records[:args.max]
    items = build_kits(records, args.outdir, args.country, args.ticket, args.weekly_extra)
    outdir = Path(args.outdir)

    n_no = sum(1 for it in items if it["needs_website"])
    print("=" * 60)
    print(" 🎯 مولّد حملة التواصل")
    print("=" * 60)
    print(f"تم تجهيز {len(items)} حزمة في: {outdir}")
    print(f"منها {n_no} بلا موقع (أولوية).")
    print(f"افتح اللوحة: {outdir / 'index.html'}")


if __name__ == "__main__":
    main()
