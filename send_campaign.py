"""
إرسال حملة التواصل تلقائياً عبر WaSenderAPI.
=============================================
يرسل الرسالة التسويقية لكل منشأة (لها واتساب) من بيانات السحب.

ضمانات مدمجة:
  • وضع تجريبي افتراضي (لا يرسل فعلياً إلا مع --send).
  • تحديد معدّل الإرسال (--rate ثوانٍ بين الرسائل) لتفادي الحظر.
  • منع التكرار: يسجّل المُرسَل في output/.campaign_sent.json ويتخطّاه لاحقاً.
  • حدّ أقصى اختياري (--limit) وإرسال فردي لكل رقم.

قبل التشغيل عيّن المفتاح:
    PowerShell:  $env:WASENDER_API_KEY = "مفتاحك"

أمثلة:
    python send_campaign.py                          # تجريبي (يعرض ما سيُرسل دون إرسال)
    python send_campaign.py --send --rate 6 --limit 20
    python send_campaign.py --link "https://USER.github.io/booking/{slug}/landing.html" --send
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from leads import load_records, normalize_phone, whatsappable
from outreach import marketing_message, _noun
from booking import slugify
from wasender import WaSenderClient, to_e164


def _feature_id(url: str) -> str:
    m = re.search(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", url or "")
    return m.group(0) if m else ""


def record_to_cloud(cloud_url: str, owner_key: str, r: dict, message: str, report_url: str = "") -> None:
    """يسجّل الإرسال في الـCRM السحابي (يحدّث حالة العميل إلى «أُرسل»). أفضل جهد."""
    if not (cloud_url and owner_key):
        return
    payload = json.dumps({
        "feature_id": _feature_id(r.get("place_url", "")), "name": r.get("name", ""),
        "phone": r.get("phone", ""), "category": r.get("category", ""),
        "website": r.get("website", ""), "source": "حملة واتساب",
        "message": (message or "")[:200], "report_url": report_url or "",
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cloud_url.rstrip("/") + "/api/crm/record", data=payload, method="POST",
        headers={"X-Owner-Key": owner_key, "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        logging.warning("تعذّر تسجيل %s في القاعدة السحابية: %s", r.get("name", ""), e)


def load_links(path: str, country: str) -> dict:
    """حمّل جدول الروابط (publish_links.csv) → {feature_id|e164: url}."""
    links: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        print(f"⚠️ ملف الروابط غير موجود: {p}")
        return links
    with p.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            url = (row.get("url") or "").strip()
            if not url:
                continue
            if row.get("feature_id"):
                links[row["feature_id"]] = url
            e164 = to_e164(row.get("phone", ""), country)
            if e164:
                links[e164] = url
    return links

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

SENT_LOG = Path("output") / ".campaign_sent.json"


def load_sent() -> dict:
    if SENT_LOG.exists():
        try:
            return json.loads(SENT_LOG.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_sent(sent: dict) -> None:
    SENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    SENT_LOG.write_text(json.dumps(sent, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="إرسال حملة التواصل عبر WaSenderAPI")
    ap.add_argument("--input", "-i", nargs="*", default=["output/*.json"])
    ap.add_argument("--country", "-c", default="966")
    ap.add_argument("--rate", type=float, default=6.0, help="ثوانٍ بين الرسائل (افتراضي 6)")
    ap.add_argument("--limit", "-n", type=int, default=0, help="حدّ أقصى للرسائل (0=الكل)")
    ap.add_argument("--link", default="", help="قالب رابط صفحة الهبوط (يدعم {slug}) يُلحق بالرسالة")
    ap.add_argument("--links-file", default="", help="ملف publish_links.csv لإرفاق رابط كل منشأة تلقائياً")
    ap.add_argument("--resend", action="store_true", help="أعد الإرسال حتى لمن سبق إرساله")
    ap.add_argument("--send", action="store_true", help="إرسال فعلي (بدونه يعمل تجريبياً)")
    ap.add_argument("--cloud-url", default=os.environ.get("BOOKING_CLOUD_URL", ""),
                    help="رابط اللوحة السحابية لتسجيل الإرسال في الـCRM (أو BOOKING_CLOUD_URL)")
    ap.add_argument("--owner-key", default=os.environ.get("BOOKING_OWNER_PASSWORD", ""),
                    help="كلمة مرور المالك لتوثيق التسجيل (أو BOOKING_OWNER_PASSWORD)")
    args = ap.parse_args()

    records = load_records(args.input)
    if not records:
        print("❌ لا توجد سجلات. اسحب أولاً ثم أعد المحاولة.")
        sys.exit(1)

    dry = not args.send
    try:
        client = WaSenderClient(min_interval=args.rate, dry_run=dry, country=args.country)
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)

    links = load_links(args.links_file, args.country) if args.links_file else {}
    sent = load_sent()
    print("=" * 60)
    print(" 📤 حملة واتساب عبر WaSenderAPI " + ("(تجريبي — لن تُرسل)" if dry else "(إرسال فعلي)"))
    if args.cloud_url and args.owner_key and not dry:
        print(" 📇 التسجيل التلقائي في الـCRM السحابي: مفعّل")
    print("=" * 60)

    stats = {"sent": 0, "skipped_sent": 0, "no_wa": 0, "failed": 0}
    for i, r in enumerate(records, 1):
        intl = normalize_phone(r.get("phone"), args.country)
        if not (intl and whatsappable(intl, r.get("phone"), args.country)):
            stats["no_wa"] += 1
            continue
        e164 = to_e164(r.get("phone"), args.country)
        if not args.resend and e164 in sent:
            stats["skipped_sent"] += 1
            print(f"  ↪ تخطّي (سبق إرساله): {r.get('name','')[:30]}")
            continue
        if args.limit and stats["sent"] >= args.limit:
            print(f"  ⏸ بلغ الحدّ الأقصى ({args.limit}).")
            break

        msg = marketing_message(r, _noun(r.get("category", "")))
        # رابط الصفحة: من ملف الروابط المنشورة (الأدق) أو من القالب
        page_link = ""
        if links:
            page_link = links.get(_feature_id(r.get("place_url", ""))) or links.get(e164, "")
        elif args.link:
            try:
                page_link = args.link.format(slug=slugify(r.get("name", ""), i))
            except Exception:
                page_link = args.link
        if page_link:
            msg += f"\nصفحة الحجز: {page_link}"

        res = client.send_text(e164, msg)
        if res.get("ok"):
            stats["sent"] += 1
            print(f"  ✓ {r.get('name','')[:30]:<30} → {e164}"
                  + ("  [تجريبي]" if res.get("dry_run") else f"  msgId={res.get('msg_id')}"))
            if not dry:
                sent[e164] = {"name": r.get("name", ""), "msg_id": res.get("msg_id"),
                              "ts": datetime.now().isoformat(timespec="seconds")}
                save_sent(sent)  # احفظ تدريجياً حتى لا يضيع التقدّم
                # تسجيل تلقائي في الـCRM السحابي (يحدّث حالة العميل إلى «أُرسل»)
                record_to_cloud(args.cloud_url, args.owner_key, r, msg, page_link)
        else:
            stats["failed"] += 1
            print(f"  ✗ {r.get('name','')[:30]:<30} → {e164}  ({res.get('error')})")

    print("\n" + "-" * 60)
    print(f"أُرسلت: {stats['sent']} | تخطّي (سبق): {stats['skipped_sent']} | "
          f"بلا واتساب: {stats['no_wa']} | فشل: {stats['failed']}")
    if dry:
        print("\n⚠️ هذا تشغيل تجريبي. أضِف --send للإرسال الفعلي.")
    print("⚠️ أرسِل رسائل فردية مهنية فقط، واحترم رغبة من يطلب التوقف (نظام حماية البيانات).")


if __name__ == "__main__":
    main()
