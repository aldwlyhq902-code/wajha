"""
نشر صفحات الهبوط تلقائياً إلى GitHub Pages.
============================================
يبني موقعاً ثابتاً نظيفاً من بيانات المنشآت، ثم ينشره على GitHub Pages،
ويُخرج روابط جاهزة للإرفاق في رسائل الحملة.

خصوصية:
  • معرّفات مبهمة (هاش) بدل أسماء الصالونات في مسار الرابط.
  • noindex + robots.txt حتى لا تُفهرس الصفحات في محرّكات البحث.
  • يُنشر فقط صفحات الحجز (لا المقترحات/الأرقام/قاعدة البيانات).

الأوامر:
    python publish.py build --input output/salons.json --base-url https://USER.github.io/REPO/
    python publish.py deploy --input output/salons.json --repo booking-demos

المخرجات:
    site/<id>/index.html        صفحة حجز لكل منشأة
    output/publish_links.csv     جدول: الاسم → الهاتف → الرابط العام
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from leads import load_records
from booking import render_page

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

NOINDEX = '<meta name="robots" content="noindex,nofollow">'
LINKS_CSV = Path("output") / "publish_links.csv"

# صفحة جذر محايدة لا تكشف قائمة العملاء
_ROOT_INDEX = (
    '<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">'
    '<meta name="robots" content="noindex,nofollow">'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
    '<title>صفحات الحجز</title>'
    '<style>body{font-family:"Segoe UI",Tahoma,sans-serif;background:#eef1f4;color:#5f6368;'
    'display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;text-align:center}'
    '</style></head><body><p>📅 صفحات الحجز — افتح رابط منشأتك المخصّص.</p></body></html>\n'
)
# إعداد Vercel (روابط نظيفة + منع الفهرسة). GitHub Pages يتجاهله.
_VERCEL_JSON = (
    '{\n  "cleanUrls": true,\n  "headers": [\n    {\n      "source": "/(.*)",\n'
    '      "headers": [ { "key": "X-Robots-Tag", "value": "noindex, nofollow" } ]\n'
    '    }\n  ]\n}\n'
)


def _force_rmtree(path) -> None:
    """حذف مجلد حتى لو احتوى ملفات للقراءة فقط (مثل كائنات .git على Windows)."""
    p = Path(path)
    if not p.exists():
        return

    def onexc(func, fp, exc):
        try:
            os.chmod(fp, stat.S_IWRITE)
            func(fp)
        except Exception:
            pass

    try:
        shutil.rmtree(p, onexc=onexc)          # Python 3.12+
    except TypeError:
        shutil.rmtree(p, onerror=onexc)        # Python ≤ 3.11 (الوسيط الثالث غير مستخدم)


def _write_links(rows) -> None:
    LINKS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with LINKS_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "phone", "feature_id", "pid", "url"])
        w.writeheader()
        w.writerows(rows)


def feature_id(url: str) -> str:
    m = re.search(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", url or "")
    return m.group(0) if m else ""


def publish_id(r: dict) -> str:
    """معرّف مبهم ثابت (ASCII) لمسار الرابط — لا يكشف اسم المنشأة."""
    key = feature_id(r.get("place_url", "")) or (r.get("name", "") + (r.get("phone", "") or ""))
    return "g" + hashlib.md5(key.encode("utf-8")).hexdigest()[:8]


def build_site(records, site_dir="site", country="966", base_url="", domain="") -> list[dict]:
    site = Path(site_dir)
    _force_rmtree(site)
    site.mkdir(parents=True, exist_ok=True)
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (site / "index.html").write_text(_ROOT_INDEX, encoding="utf-8")   # جذر محايد
    (site / "vercel.json").write_text(_VERCEL_JSON, encoding="utf-8")  # إعداد Vercel
    if domain:
        # ملف CNAME يخبر GitHub Pages بالنطاق المخصّص
        (site / "CNAME").write_text(domain.strip() + "\n", encoding="utf-8")
        base_url = base_url or f"https://{domain.strip()}/"

    rows, seen = [], set()
    for r in records:
        pid = publish_id(r)
        if pid in seen:
            continue
        seen.add(pid)
        page = render_page(r, country).replace("<head>", "<head>" + NOINDEX, 1)
        (site / pid).mkdir(parents=True, exist_ok=True)
        (site / pid / "index.html").write_text(page, encoding="utf-8")
        url = (base_url.rstrip("/") + "/" + pid + "/") if base_url else ""
        rows.append({"name": r.get("name", ""), "phone": r.get("phone", ""),
                     "feature_id": feature_id(r.get("place_url", "")), "pid": pid, "url": url})

    _write_links(rows)
    return rows


# --------------------------------------------------------------------------- #
def _run(cmd, cwd=None, inp=None):
    return subprocess.run(cmd, cwd=cwd, input=inp, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def gh_owner() -> str:
    r = _run(["gh", "api", "user", "-q", ".login"])
    if r.returncode != 0:
        raise RuntimeError("gh غير مُسجّل الدخول. شغّل: gh auth login")
    return r.stdout.strip()


def dns_help(domain: str, owner: str) -> None:
    domain = domain.strip().rstrip(".")
    labels = domain.split(".")
    gtlds = {"com", "net", "org", "io", "dev", "app", "co", "ai", "me", "xyz", "sa", "shop", "store"}
    # نطاق فرعي مؤكّد فقط حين: label.registrable.gtld (≥3 أجزاء، آخرها لاحقة معروفة)
    is_subdomain = len(labels) >= 3 and labels[-1] in gtlds and len(labels[-2]) > 2
    pages_ips = ("185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153")
    print("\n📡 أضِف سجلات DNS التالية لدى مزوّد نطاقك:")
    if is_subdomain:
        print(f"     CNAME   {labels[0]}   {owner}.github.io.")
    else:
        # نطاق جذر (apex) — مع لاحقات متعددة المقاطع (co.uk) نعرض الخيارين للأمان
        print(f"  • إن كان ({domain}) نطاقاً جذرياً — سجلات A إلى عناوين GitHub Pages:")
        for ip in pages_ips:
            print(f"     A      @      {ip}")
        print(f"     CNAME  www  {owner}.github.io.")
        print(f"  • إن كان نطاقاً فرعياً — بدلاً من ذلك:  CNAME  {labels[0]}  {owner}.github.io.")
    print("ثم في إعدادات المستودع › Pages فعّل «Enforce HTTPS» (يُصدر الشهادة خلال دقائق).")


def deploy(site_dir: str, repo: str, domain: str = "") -> tuple[str, bool]:
    site = Path(site_dir).resolve()
    owner = gh_owner()
    email = f"{owner}@users.noreply.github.com"

    # مستودع git جديد في مجلد الموقع (نشر بدفع قسري ليعكس الحالة الحالية)
    _force_rmtree(site / ".git")
    _run(["git", "init", "-b", "main"], cwd=site)
    _run(["git", "add", "-A"], cwd=site)
    _run(["git", "-c", f"user.email={email}", "-c", f"user.name={owner}",
          "commit", "-m", "نشر صفحات الحجز"], cwd=site)

    full = f"{owner}/{repo}"
    cr = _run(["gh", "repo", "create", full, "--public", "--source", ".",
               "--remote", "origin", "--push"], cwd=site)
    if cr.returncode != 0:
        # ميّز «المستودع موجود مسبقاً» عن أي فشل آخر (صلاحيات/اسم/شبكة)
        err = (cr.stderr or "").lower()
        if "already exists" not in err and "name already exists" not in err:
            raise RuntimeError(f"فشل إنشاء المستودع {full}: {cr.stderr.strip()}")
        # موجود مسبقاً → أضف الريموت وادفع قسرياً
        _run(["git", "remote", "remove", "origin"], cwd=site)
        _run(["git", "remote", "add", "origin", f"https://github.com/{full}.git"], cwd=site)
        push = _run(["git", "push", "-u", "origin", "main", "--force"], cwd=site)
        if push.returncode != 0:
            raise RuntimeError(f"فشل الدفع: {push.stderr.strip() or cr.stderr.strip()}")

    # فعّل GitHub Pages (تجاهل فقط حالة «مفعّلة مسبقاً»)
    pr = _run(["gh", "api", "-X", "POST", f"repos/{full}/pages", "--input", "-"],
              inp=json.dumps({"source": {"branch": "main", "path": "/"}}))
    if pr.returncode != 0:
        err = (pr.stderr or "").lower()
        if not any(s in err for s in ("already", "409", "422")):
            raise RuntimeError("فشل تفعيل GitHub Pages: "
                               + (pr.stderr.strip() or pr.stdout.strip()))

    # اضبط النطاق المخصّص (إضافةً لملف CNAME المرفوع)
    cname_ok = True
    if domain:
        cr2 = _run(["gh", "api", "--method", "PUT", f"repos/{full}/pages",
                    "-f", f"cname={domain.strip()}"])
        if cr2.returncode != 0:
            cname_ok = False
            print("⚠️ لم يقبل GitHub ضبط النطاق عبر API: "
                  + (cr2.stderr.strip() or cr2.stdout.strip()))
            print("   (سيعتمد على ملف CNAME المرفوع؛ تحقّق من DNS وإعدادات Pages.)")

    return f"https://{owner}.github.io/{repo}/", cname_ok


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="نشر صفحات الهبوط إلى GitHub Pages")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="بناء الموقع محلياً فقط")
    b.add_argument("--input", "-i", nargs="*", default=["output/*.json"])
    b.add_argument("--country", "-c", default="966")
    b.add_argument("--site", default="site")
    b.add_argument("--base-url", default="", help="رابط الأساس لحساب الروابط الكاملة")
    b.add_argument("--domain", default="", help="نطاق مخصّص (يكتب CNAME ويحسب الروابط به)")

    d = sub.add_parser("deploy", help="بناء + نشر على GitHub Pages")
    d.add_argument("--input", "-i", nargs="*", default=["output/*.json"])
    d.add_argument("--country", "-c", default="966")
    d.add_argument("--site", default="site")
    d.add_argument("--repo", default="booking-demos", help="اسم مستودع GitHub")
    d.add_argument("--domain", default="", help="نطاق مخصّص (مثال: booking.example.com)")
    args = ap.parse_args()

    records = load_records(args.input)
    if not records:
        print("❌ لا توجد بيانات. اسحب أولاً.")
        sys.exit(1)

    if args.cmd == "build":
        rows = build_site(records, args.site, args.country, args.base_url, args.domain)
        print(f"✓ بُني الموقع: {len(rows)} صفحة في {args.site}/")
        print(f"✓ الروابط: {LINKS_CSV}")
        if args.base_url or args.domain:
            for r in rows[:5]:
                print(f"  {r['name'][:30]:<30} → {r['url']}")
        return

    # deploy
    print("=" * 60)
    print(" 🚀 نشر صفحات الهبوط على GitHub Pages")
    print("=" * 60)
    rows = build_site(records, args.site, args.country, domain=args.domain)  # يكتب CNAME إن وُجد نطاق
    print(f"بُني {args.site}/ … جارٍ النشر إلى GitHub …")
    base, cname_ok = deploy(args.site, args.repo, args.domain)
    use_domain = bool(args.domain) and cname_ok
    final_base = f"https://{args.domain.strip()}/" if use_domain else base
    # أعِد كتابة الروابط بالأساس النهائي دون إعادة بناء الصفحات (حتى لا نمسح .git)
    for r in rows:
        r["url"] = final_base.rstrip("/") + "/" + r["pid"] + "/"
    _write_links(rows)
    print("\n✅ تم النشر")
    print(f"الرابط الأساس: {final_base}")
    print(f"عدد الصفحات : {len(rows)}")
    print(f"جدول الروابط: {LINKS_CSV}")
    print("\nأمثلة روابط جاهزة للإرسال:")
    for r in rows[:5]:
        print(f"  {r['name'][:28]:<28} → {r['url']}")
    if args.domain:
        owner = gh_owner()
        dns_help(args.domain, owner)
    print("\nأرسِل الحملة مع الروابط:")
    print(f"  python send_campaign.py --links-file {LINKS_CSV} --send")
    print("\n(قد تستغرق الصفحات ~دقيقة لتصبح حيّة. لحذف الاستضافة: gh repo delete <owner>/"
          + args.repo + " --yes)")


if __name__ == "__main__":
    main()
