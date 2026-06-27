"""
المتابعة الآلية — تذكير لمن أُرسل لهم ولم يردّوا منذ N أيام.
============================================================
يقرأ العملاء من الـCRM السحابي (حالة «أُرسل»)، ويرسل رسالة متابعة لطيفة لمن
مرّ على آخر تواصل معهم ≥ N أيام، ثم يسجّل المتابعة في القاعدة. يحترم opt-out
(لا يلمس «موقوف/مرفوض/عميل/مهتم/ردّ» — يقتصر على «أُرسل»).

قبل التشغيل عيّن:
    $env:WASENDER_API_KEY        = "مفتاحك"
    $env:BOOKING_CLOUD_URL       = "https://booking-system-y5id.onrender.com"
    $env:BOOKING_OWNER_PASSWORD  = "كلمة مرور المالك"

أمثلة:
    python followup.py                      # تجريبي (يعرض من سيُتابَع)
    python followup.py --send --days 3 --rate 8 --limit 20
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime

from wasender import WaSenderClient, to_e164

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")


def fetch_prospects(cloud: str, key: str, status: str = "أُرسل") -> list[dict]:
    url = cloud.rstrip("/") + "/api/owner/prospects?status=" + urllib.parse.quote(status)
    req = urllib.request.Request(url, headers={"X-Owner-Key": key})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8")).get("prospects", [])


def record_back(cloud: str, key: str, p: dict, message: str) -> None:
    payload = json.dumps({
        "feature_id": p.get("feature_id", ""), "name": p.get("name", ""),
        "phone": p.get("phone", ""), "category": p.get("category", ""),
        "website": p.get("website", ""), "source": "متابعة", "message": message[:200],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(cloud.rstrip("/") + "/api/crm/record", data=payload, method="POST",
                                 headers={"X-Owner-Key": key, "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        logging.warning("تعذّر تسجيل المتابعة لـ %s: %s", p.get("name", ""), e)


def followup_message(p: dict) -> str:
    name = p.get("name") or "منشأتكم"
    if (p.get("opens") or 0) > 0:
        return (f"مرحباً {name} 👋 لاحظنا اطّلاعكم على صفحتكم التي أعددناها — يسعدنا الإجابة "
                f"عن أي استفسار وتفعيل الحجز عبر واتساب لكم. هل نحدّد مكالمة قصيرة؟")
    return (f"مرحباً {name} 👋 هل أتيحت لكم فرصة الاطّلاع على صفحة الحجز التي جهّزناها لكم؟ "
            f"يسعدنا خدمتكم والإجابة عن أي سؤال. متى يناسبكم؟")


def days_since(iso_ts: str, now: datetime) -> float | None:
    if not iso_ts:
        return None
    try:
        return (now - datetime.fromisoformat(iso_ts)).total_seconds() / 86400.0
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="المتابعة الآلية عبر WaSenderAPI")
    ap.add_argument("--cloud-url", default=os.environ.get("BOOKING_CLOUD_URL", ""))
    ap.add_argument("--owner-key", default=os.environ.get("BOOKING_OWNER_PASSWORD", ""))
    ap.add_argument("--days", type=float, default=3.0, help="حدّ أدنى لعدد الأيام منذ آخر تواصل")
    ap.add_argument("--country", "-c", default="966")
    ap.add_argument("--rate", type=float, default=8.0, help="ثوانٍ بين الرسائل")
    ap.add_argument("--limit", "-n", type=int, default=0, help="حدّ أقصى (0=الكل)")
    ap.add_argument("--send", action="store_true", help="إرسال فعلي (بدونه تجريبي)")
    args = ap.parse_args()

    if not (args.cloud_url and args.owner_key):
        print("❌ عيّن BOOKING_CLOUD_URL و BOOKING_OWNER_PASSWORD (أو مرّر --cloud-url/--owner-key).")
        sys.exit(1)

    try:
        prospects = fetch_prospects(args.cloud_url, args.owner_key)
    except Exception as e:
        print(f"❌ تعذّر جلب العملاء من السحابة: {e}")
        sys.exit(1)

    now = datetime.now()
    candidates = []
    for p in prospects:
        d = days_since(p.get("last_contacted_at"), now)
        if d is not None and d >= args.days:
            candidates.append(p)

    dry = not args.send
    print("=" * 60)
    print(" 🔁 المتابعة الآلية " + ("(تجريبي — لن تُرسل)" if dry else "(إرسال فعلي)"))
    print(f" مرشّحون للمتابعة (أُرسل ومضى ≥ {args.days} يوم): {len(candidates)} من {len(prospects)}")
    print("=" * 60)
    if not candidates:
        print("لا يوجد من يحتاج متابعة الآن."); return

    try:
        client = WaSenderClient(min_interval=args.rate, dry_run=dry, country=args.country)
    except RuntimeError as e:
        print(f"❌ {e}"); sys.exit(1)

    sent = failed = 0
    for p in candidates:
        if args.limit and sent >= args.limit:
            print(f"  ⏸ بلغ الحدّ ({args.limit})."); break
        num = (p.get("whatsapp") or "").strip() or to_e164(p.get("phone"), args.country)
        if not num:
            continue
        msg = followup_message(p)
        res = client.send_text(num, msg)
        if res.get("ok"):
            sent += 1
            tag = "👁" if (p.get("opens") or 0) > 0 else ""
            print(f"  ✓ {p.get('name','')[:30]:<30} {tag}"
                  + ("  [تجريبي]" if res.get("dry_run") else f"  msgId={res.get('msg_id')}"))
            if not dry:
                record_back(args.cloud_url, args.owner_key, p, msg)
        else:
            failed += 1
            print(f"  ✗ {p.get('name','')[:30]:<30} ({res.get('error')})")

    print("\n" + "-" * 60)
    print(f"متابعات أُرسلت: {sent} | فشل: {failed}")
    if dry:
        print("⚠️ تجريبي. أضِف --send للإرسال الفعلي.")
    print("⚠️ احترم opt-out — من يطلب التوقف انقله لحالة «موقوف» فلا يُتابَع.")


if __name__ == "__main__":
    main()
