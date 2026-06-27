"""
تقييم مواقع المنشآت + عرض خدمات (إنشاء/تحسين موقع + واتساب).
============================================================
لكل منشأة من بيانات السحب: يفحص موقعها الحالي ويُعطي تقييماً ومشاكل،
ثم يقترح الخدمة المناسبة ويُجهّز رسالة واتساب مخصّصة.

الفحوصات:
  • بلا موقع            → فرصة «إنشاء موقع».
  • حساب تواصل فقط      → (انستقرام/سناب/منصة حجز) → يحتاج موقعاً مملوكاً.
  • معطّل/لا يفتح        → فرصة «إعادة بناء».
  • بلا HTTPS / غير متوافق مع الجوال / بطيء → فرص «تحسين».

التشغيل:
    python webaudit.py                         # كل output/*.json
    python webaudit.py --input output/x.json --workers 12

يُنتج:  output/webaudit_<وقت>.html  (تقرير)  +  .csv
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from leads import load_records, normalize_phone, whatsappable

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# نطاقات «ليست موقعاً مملوكاً» (تواصل/منصات خارجية)
SOCIAL = ("instagram.com", "facebook.com", "fb.com", "snapchat.com", "tiktok.com",
          "twitter.com", "x.com", "linktr.ee", "beacons.ai", "wa.me", "t.me",
          "youtube.com", "pinterest.com", "linktree", "bit.ly", "google.com",
          "maps.app.goo.gl", "wevyin.com", "hsmrt.com", "rest-menu", "qr-menu")


def _host(url: str) -> str:
    u = url if "//" in url else "http://" + url
    return (urlparse(u).netloc or "").lower()


def audit_site(url: str, timeout: float = 8.0) -> dict:
    r = {"url": url, "has_site": True, "reachable": False, "https": False,
         "mobile": False, "social": False, "status": None, "ms": None, "issues": []}
    host = _host(url)
    r["social"] = any(s in host for s in SOCIAL)
    if r["social"]:
        r["issues"].append("حساب تواصل/منصة خارجية فقط (ليس موقعاً مملوكاً)")
        return r

    fetch = url if url.lower().startswith(("http://", "https://")) else "https://" + url
    t0 = time.time()
    html_text = ""
    try:
        req = urllib.request.Request(fetch, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            r["status"] = resp.status
            r["reachable"] = True
            r["https"] = resp.geturl().lower().startswith("https")
            html_text = resp.read(60000).decode("utf-8", "replace")
    except ssl.SSLError:
        r["issues"].append("شهادة الأمان (SSL) غير صالحة")
        try:  # هل يعمل أصلاً بتجاوز التحقق؟
            ctx = ssl._create_unverified_context()
            req = urllib.request.Request(fetch, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                r["status"] = resp.status
                r["reachable"] = True
                html_text = resp.read(60000).decode("utf-8", "replace")
        except Exception:
            pass
    except urllib.error.HTTPError as e:
        r["status"] = e.code
        r["reachable"] = e.code < 500
        r["https"] = fetch.lower().startswith("https")
    except Exception:
        r["issues"].append("الموقع لا يفتح (معطّل أو بطيء جداً)")
    r["ms"] = int((time.time() - t0) * 1000)

    if r["reachable"]:
        low = html_text.lower()
        r["mobile"] = ('name="viewport"' in low) or ("name='viewport'" in low)
        if not r["https"]:
            r["issues"].append("غير آمن (بلا HTTPS)")
        if not r["mobile"]:
            r["issues"].append("غير متوافق مع الجوال")
        if r["ms"] and r["ms"] > 3500:
            r["issues"].append("بطيء التحميل")
    return r


# وصف خدمة الواتساب (عدّله أو اذكر اسم أداتك مثل whats_bot)
WA_SERVICE = "خدمة واتساب للحجز واستقبال الطلبات تلقائياً"


def grade_and_offer(r_has_site: bool, a: dict) -> tuple[str, str, str]:
    """يُعيد (التقييم، الفرصة، العرض)."""
    if not r_has_site:
        return ("بلا موقع ✗", "إنشاء موقع",
                f"إنشاء موقع احترافي متوافق مع الجوال + {WA_SERVICE}")
    if a["social"]:
        return ("ضعيف (تواصل فقط)", "إنشاء موقع مملوك",
                f"إنشاء موقع رسمي مملوك لكم + {WA_SERVICE}")
    if not a["reachable"]:
        return ("معطّل ✗", "إعادة بناء",
                f"إعادة بناء موقعكم (الحالي لا يعمل) + {WA_SERVICE}")
    nissues = sum(1 for x in ("غير آمن (بلا HTTPS)", "غير متوافق مع الجوال", "بطيء التحميل") if x in a["issues"])
    if nissues == 0:
        return ("ممتاز ✓", "خدمة واتساب",
                f"{WA_SERVICE} + تحسينات تسويقية")
    label = "جيد" if nissues == 1 else "ضعيف"
    fix = "، ".join(a["issues"])
    return (f"{label} (يحتاج تحسين)", "تحسين الموقع",
            f"تحسين موقعكم ({fix}) + {WA_SERVICE}")


def _audit_or_empty(site: str, timeout: float) -> tuple[bool, dict]:
    if site:
        return True, audit_site(site, timeout)
    return False, {"social": False, "reachable": False, "https": False,
                   "mobile": False, "issues": [], "ms": None, "status": None}


def build_message(r: dict, country: str = "966", timeout: float = 8.0) -> str:
    """رسالة واتساب مخصّصة (تقييم الموقع + عرض الخدمة) — يستخدمها الإرسال الجماعي."""
    site = (r.get("website") or "").strip()
    has, a = _audit_or_empty(site, timeout)
    grade, _opp, offer = grade_and_offer(has, a)
    return whatsapp_msg(r, grade, offer, has, a)


def whatsapp_msg(r: dict, grade: str, offer: str, has_site: bool, a: dict) -> str:
    name = r.get("name") or "منشأتكم"
    rating, reviews = r.get("rating"), r.get("reviews_count")
    praise = f" (تقييمكم {rating}⭐ و{reviews} مراجعة يستحق حضوراً رقمياً أفضل)" if rating and reviews else ""
    if not has_site:
        finding = "لاحظتُ أن منشأتكم بلا موقع إلكتروني"
    elif a["social"]:
        finding = "لاحظتُ أنكم تعتمدون على حساب تواصل فقط بدون موقع رسمي مملوك"
    elif not a["reachable"]:
        finding = "لاحظتُ أن موقعكم الحالي لا يفتح"
    else:
        finding = "اطّلعتُ على موقعكم ولاحظتُ نقاطاً تحتاج تحسيناً: " + "، ".join(a["issues"])
    return (f"مرحباً {name} 👋\n{finding}{praise}.\n"
            f"أقدّم خدمة: {offer}.\nهل يناسبكم عرض سريع خلال دقيقتين؟")


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def audit_record(r: dict, country: str, timeout: float) -> dict:
    site = (r.get("website") or "").strip()
    has_site = bool(site)
    a = audit_site(site, timeout) if has_site else {"social": False, "reachable": False,
                                                    "https": False, "mobile": False,
                                                    "issues": [], "ms": None, "status": None}
    grade, opp, offer = grade_and_offer(has_site, a)
    intl = normalize_phone(r.get("phone"), country)
    wa = ""
    if intl and whatsappable(intl, r.get("phone"), country):
        wa = f"https://wa.me/{intl}?text={quote(whatsapp_msg(r, grade, offer, has_site, a))}"
    # أولوية: بلا موقع/تواصل/معطّل = الأعلى
    pr = 3 if (not has_site or a.get("social") or not a.get("reachable")) else (2 if a.get("issues") else 1)
    return {"name": r.get("name", ""), "category": r.get("category", ""),
            "rating": r.get("rating"), "reviews": r.get("reviews_count"),
            "phone": r.get("phone", ""), "website": site,
            "grade": grade, "opportunity": opp, "offer": offer,
            "issues": "، ".join(a["issues"]) or "—", "wa": wa, "priority": pr,
            "status": a.get("status"), "ms": a.get("ms")}


# --------------------------------------------------------------------------- #
def render_html(rows: list[dict]) -> str:
    n_no = sum(1 for r in rows if "بلا موقع" in r["grade"])
    n_weak = sum(1 for r in rows if r["priority"] >= 2)
    cards = []
    for r in rows:
        wa = (f'<a class="wa" href="{_e(r["wa"])}" target="_blank">واتساب</a>' if r["wa"] else "")
        site = (f'<a href="{_e(r["website"])}" target="_blank">الموقع</a>' if r["website"] else '<span class="no">بلا موقع</span>')
        badge = "hi" if r["priority"] == 3 else ("mid" if r["priority"] == 2 else "lo")
        cards.append(f"""<tr>
  <td><span class="g {badge}">{_e(r['grade'])}</span></td>
  <td><strong>{_e(r['name'])}</strong><div class="m">{_e(r['category'])}{' · '+_e(r['rating'])+'⭐' if r['rating'] else ''}</div></td>
  <td class="m">{_e(r['issues'])}</td>
  <td><strong>{_e(r['opportunity'])}</strong><div class="m">{_e(r['offer'])}</div></td>
  <td>{site}</td>
  <td class="act">{wa}</td>
</tr>""")
    return """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>تقييم المواقع</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:"Segoe UI",Tahoma,sans-serif}
body{background:#eef1f4;color:#1f2933;padding:24px}
h1{font-size:24px}.muted{color:#7b8794;margin-bottom:16px}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.kpi{background:#fff;border:1px solid #e3e8ee;border-radius:14px;padding:14px 18px;min-width:140px}
.kpi b{display:block;font-size:24px;color:#1a73e8}.kpi.r b{color:#ea4335}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e8ee;border-radius:14px;overflow:hidden}
th,td{padding:11px 12px;text-align:right;border-bottom:1px solid #eef1f4;font-size:14px;vertical-align:top}
th{background:#f8f9fa;color:#5f6368;font-size:13px}.m{font-size:12px;color:#7b8794}
.g{display:inline-block;padding:4px 10px;border-radius:20px;color:#fff;font-weight:700;font-size:12px;white-space:nowrap}
.g.hi{background:#ea4335}.g.mid{background:#fbbc04;color:#202124}.g.lo{background:#34a853}
.no{color:#ea4335}.act{white-space:nowrap}
a{color:#1a73e8;text-decoration:none}.wa{background:#25d366;color:#fff!important;padding:6px 12px;border-radius:8px}
</style></head><body>
<h1>🔎 تقييم مواقع المنشآت — عروض خدماتك</h1>
<p class="muted">الأحمر = أولوية عالية (بلا موقع/تواصل فقط/معطّل). اضغط «واتساب» لرسالة عرض جاهزة.</p>
<div class="kpis">
  <div class="kpi"><b>__N__</b>إجمالي</div>
  <div class="kpi r"><b>__NO__</b>بلا موقع</div>
  <div class="kpi r"><b>__WEAK__</b>تحتاج إنشاء/تحسين</div>
</div>
<table><thead><tr><th>التقييم</th><th>المنشأة</th><th>المشاكل</th><th>الفرصة / العرض</th><th>الموقع</th><th></th></tr></thead>
<tbody>__ROWS__</tbody></table>
</body></html>""".replace("__N__", str(len(rows))).replace("__NO__", str(n_no)).replace("__WEAK__", str(n_weak)).replace("__ROWS__", "".join(cards))


def main() -> None:
    ap = argparse.ArgumentParser(description="تقييم مواقع المنشآت + عروض خدمات")
    ap.add_argument("--input", "-i", nargs="*", default=["output/*.json"])
    ap.add_argument("--country", "-c", default="966")
    ap.add_argument("--workers", type=int, default=10, help="عدد الفحوصات المتوازية")
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--max", "-n", type=int, default=200)
    args = ap.parse_args()

    records = load_records(args.input)[:args.max]
    if not records:
        print("❌ لا توجد سجلات. اسحب أولاً.")
        sys.exit(1)

    print(f"جارٍ تقييم مواقع {len(records)} منشأة …")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(lambda r: audit_record(r, args.country, args.timeout), records))
    rows.sort(key=lambda x: (x["priority"], (x["reviews"] or 0)), reverse=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    html_path = Path("output") / f"webaudit_{stamp}.html"
    csv_path = html_path.with_suffix(".csv")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(rows), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["grade", "name", "category", "rating", "reviews",
                                          "phone", "website", "opportunity", "offer", "issues", "wa"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in w.fieldnames})

    no_site = sum(1 for r in rows if "بلا موقع" in r["grade"])
    weak = sum(1 for r in rows if r["priority"] >= 2)
    print("=" * 60)
    print(f" 🔎 تقييم المواقع: {len(rows)} منشأة")
    print(f"   بلا موقع: {no_site}  |  تحتاج إنشاء/تحسين: {weak}")
    print("\n--- أعلى الفرص ---")
    for r in rows[:10]:
        print(f"  [{r['grade']:<18}] {r['name'][:28]:<28} | {r['opportunity']}")
    print(f"\n✓ التقرير: {html_path}")
    print(f"✓ CSV   : {csv_path}")


if __name__ == "__main__":
    main()
