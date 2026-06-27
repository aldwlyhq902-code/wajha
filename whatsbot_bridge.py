"""
whatsbot_bridge.py — جسر خفيف يربط منصّة الخرائط (Google Map SaaS) بمشروع whats_bot.
============================================================================================

الغرض
-----
عندما «يوافق» عميل محتمَل (prospect) عبر مسار التوعية في منصّة الخرائط — أي صار قابلاً
للواتساب وأبدى موافقة صريحة — نريد «تسليمه» إلى whats_bot ليُدار بعدها كجهة اتصال
داخل محرّك الحملات (مع ضوابط opt-in / quiet-hours / frequency-cap الموجودة هناك).

هذا الملف لا يخترع أي واجهة. هو مبنيّ على قراءة فعليّة لمشروع whats_bot في D:\\whats_bot:
  - whats_bot هو skill بنية سداسية (Hexagonal): core نقيّ + ports + adapters، يُبنى بـ tsup.
    لا يعرض REST عامًّا لإدخال جهات اتصال؛ المسار الوحيد عبر HTTP هو /webhook الوارد من
    المزوّد (مُوقَّع HMAC، الترويسة x-webhook-signature) — وهو ليس مسار إدخال بيانات.
  - القاعدة Supabase/Postgres ضمن schema «wa» متعدّد المستأجرين (RLS worker-only)،
    والكتابة تتمّ عبر service_role وRPCs (مثل setMarketingOptIn) داخل عمّال Node.
    الجداول ذات الصلة (db/migrations/0001 + 0012 + 0018/0019/0020):
        wa.tenants(id, name, wa_session_id, wa_number, …)
        wa.contacts(id, tenant_id, phone, name, marketing_opt_in, marketing_opted_out_at, …)
        wa.campaigns / wa.campaign_recipients (محرّك الحملات: claim/complete/release/defer + freq-cap)
        wa.campaign_blocklist (حظر صلب فوق opt-out)
    عمود wa.contacts.marketing_opt_in هو بوّابة الموافقة التي يفحصها التجسيد والإرسال.
  - المزوّد مشترك: WaSenderAPI على WA_BASE_URL (افتراضيًّا https://www.wasenderapi.com/api).
    الإرسال: POST /send-message بترويسة Authorization: Bearer <api_key لكل جلسة>.
    إدارة الجلسات تستخدم WASENDER_PAT. (منصّة الخرائط تستخدم نفس المزوّد عبر wasender.py.)

خيارات الربط (Integration options)
----------------------------------
(1) عبر قاعدة whats_bot / RPC  ← **الموصى به**
    upsert في wa.contacts للمستأجر المقصود ثم ضبط marketing_opt_in=true (RPC setMarketingOptIn).
    هذا هو «مصدر الحقيقة» الذي يحترمه محرّك الحملات وضوابطه (opt-in/quiet-hours/freq-cap/blocklist).
    التنفيذ هنا اختياريّ عبر PostgREST (schema=wa) بمفتاح service_role — يُفعَّل فقط إذا ضُبطت
    WHATSBOT_SUPABASE_URL + WHATSBOT_SUPABASE_SERVICE_ROLE_KEY + WHATSBOT_TENANT_ID. وإلا فـ stub موثّق.
    مزاياه: يدخل الضوابط فورًا، idempotent على (tenant_id, phone)، لا يتجاوز موافقة العميل.
    تحذيره: مفتاح service_role حسّاس (خادم فقط)، ومعرّف المستأجر يجب أن يطابق العميل الصحيح.

(2) عبر WaSenderAPI المشترك
    إرسال الرسالة الأولى مباشرة بنفس المزوّد (كما يفعل wasender.py). يعمل «اليوم» بلا قاعدة،
    لكنّه **يتجاوز** opt-in/quiet-hours/frequency-cap وسجلّ الحملات في whats_bot — لذا لا يُنصَح به
    لتسليم العملاء؛ هو مناسب فقط لرسالة تأكيد لمرّة واحدة خارج محرّك الحملات.

(3) ويبهوك (Webhook) تستقبله طبقة تكامل في whats_bot
    تبني نقطة HTTP صغيرة (Fastify route) جنب handle-webhook تستقبل «prospect وافق» وتكتب في
    القاعدة بنفس منطق الخيار (1). مرن ويعزل المفتاح عن منصّة الخرائط، لكنّه يتطلّب كودًا جديدًا
    في whats_bot (غير موجود الآن) — لذلك هو هدف مستقبليّ، وحتى يُبنى تبقى الدالة هنا stub آمنًا.

التوصية: ابدأ بالخيار (1) عبر PostgREST/RPC على قاعدة whats_bot (idempotent، يحترم الموافقة
والضوابط). عند نضج العمليّات، انقل نقطة الربط خلف ويبهوك (3) لإخفاء المفتاح. تجنّب (2) للتسليم.

العقد العامّ
-----------
    handoff_prospect(prospect: dict, *, mode: str | None = None) -> dict
        يُعيد {ok, action, note, ...}. آمن دائمًا: في sandbox/dry لا اتصال شبكيّ.

البيئة المستخدَمة (stdlib urllib فقط — لا مكتبات خارجيّة)
    BOT_MODE                            : sandbox (افتراضي، لا إرسال) أو live
    WA_BASE_URL                         : افتراضي https://www.wasenderapi.com/api
    WASENDER_PAT أو WASENDER_API_KEY    : مفتاح المزوّد المشترك (للخيار 2 إن لزم)
    WHATSBOT_SUPABASE_URL               : (اختياري) رابط Supabase لقاعدة whats_bot (الخيار 1)
    WHATSBOT_SUPABASE_SERVICE_ROLE_KEY  : (اختياري) مفتاح service_role (خادم فقط)
    WHATSBOT_TENANT_ID                  : (اختياري) معرّف المستأجر المقصود في wa.tenants

ملاحظة أمان: لا نُسجّل المفاتيح ولا أرقامًا كاملة في السجلّ؛ ونفشل-مغلقًا عند نقص الإعداد في live.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request

# إعادة توجيه الإخراج لـ UTF-8 (مطلوب على Windows لتفادي انهيار الترميز).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# إعادة استخدام منطق الأرقام من منصّة الخرائط (إن توفّر؛ وإلا fallbacks آمنة).
try:
    from leads import normalize_phone, whatsappable  # type: ignore
except Exception:  # pragma: no cover - يُستخدم فقط إن استُورد الملف خارج المشروع
    import re as _re

    def normalize_phone(raw, default_cc: str = "966"):  # type: ignore
        if not raw:
            return None
        has_plus = str(raw).strip().startswith("+")
        digits = _re.sub(r"\D", "", str(raw))
        if not digits:
            return None
        if has_plus:
            return digits
        if digits.startswith("00"):
            return digits[2:]
        if digits.startswith("0"):
            return default_cc + digits[1:]
        if digits.startswith(default_cc):
            return digits
        return default_cc + digits

    def whatsappable(intl, raw, default_cc):  # type: ignore
        if not intl:
            return False
        nat = intl[len(default_cc):] if intl.startswith(default_cc) else intl
        if default_cc == "966" and len(nat) == 9 and nat.startswith("5"):
            return True
        return bool(raw and str(raw).strip().startswith("+") and not intl.startswith(default_cc))


logger = logging.getLogger("whatsbot_bridge")

DEFAULT_WA_BASE_URL = "https://www.wasenderapi.com/api"


# --------------------------------------------------------------------------- #
# قراءة البيئة / الإعداد                                                       #
# --------------------------------------------------------------------------- #
def _env(name: str) -> str | None:
    val = os.environ.get(name)
    return val.strip() if val and val.strip() else None


def get_mode(explicit: str | None = None) -> str:
    """وضع التشغيل: 'live' فقط إذا طُلب صراحةً (وسيط أو BOT_MODE=live)، وإلا 'sandbox'."""
    raw = (explicit or _env("BOT_MODE") or "sandbox").lower()
    return "live" if raw == "live" else "sandbox"


def wa_base_url() -> str:
    return _env("WA_BASE_URL") or DEFAULT_WA_BASE_URL


def provider_key() -> str | None:
    """مفتاح المزوّد المشترك (WaSenderAPI): PAT أوّلاً ثم API_KEY."""
    return _env("WASENDER_PAT") or _env("WASENDER_API_KEY")


def _mask(value: str | None) -> str:
    """إخفاء سرّ/رقم في السجلّ (نُبقي آخر رقمين فقط للتشخيص)."""
    if not value:
        return "—"
    s = str(value)
    return ("*" * max(0, len(s) - 2)) + s[-2:] if len(s) > 2 else "**"


def supabase_config() -> dict | None:
    """إعداد الخيار (1): قاعدة whats_bot عبر PostgREST. يُعيد None إن لم يكتمل."""
    url = _env("WHATSBOT_SUPABASE_URL")
    key = _env("WHATSBOT_SUPABASE_SERVICE_ROLE_KEY")
    tenant = _env("WHATSBOT_TENANT_ID")
    if url and key and tenant:
        return {"url": url.rstrip("/"), "key": key, "tenant_id": tenant}
    return None


# --------------------------------------------------------------------------- #
# تطبيع العميل المحتمَل                                                        #
# --------------------------------------------------------------------------- #
def normalize_prospect(prospect: dict, *, default_cc: str = "966") -> dict:
    """
    يستخرج ويُطبّع حقول العميل المطلوبة للتسليم:
      name, phone_raw, phone_e164 (بصيغة +)، phone_intl (أرقام فقط)، whatsappable، consent.
    يقبل مفاتيح متعدّدة شائعة في سجلّات منصّة الخرائط.
    """
    if not isinstance(prospect, dict):
        prospect = {}

    def pick(*keys, default=None):
        for k in keys:
            v = prospect.get(k)
            if v not in (None, "", []):
                return v
        return default

    name = pick("name", "title", "business_name", "company", default="") or ""
    phone_raw = pick("phone", "phone_raw", "tel", "mobile", "whatsapp", default="") or ""
    intl = normalize_phone(str(phone_raw), default_cc) if phone_raw else None
    e164 = ("+" + intl) if intl else None
    can_wa = whatsappable(intl, str(phone_raw) if phone_raw else None, default_cc)

    # الموافقة: نقبل عدّة أشكال صريحة. الافتراض False (فشل-مغلق على الموافقة).
    consent_raw = pick("consent", "opt_in", "marketing_opt_in", "approved", default=False)
    consent = consent_raw in (True, 1, "1", "true", "True", "yes", "نعم", "وافق", "approved")

    return {
        "name": str(name).strip(),
        "phone_raw": str(phone_raw).strip(),
        "phone_intl": intl,
        "phone_e164": e164,
        "whatsappable": bool(can_wa),
        "consent": bool(consent),
        "tenant_id": pick("tenant_id", "whatsbot_tenant_id", default=None),
        "source": pick("source", default="google_map"),
    }


# --------------------------------------------------------------------------- #
# الخيار (1): تسليم إلى قاعدة whats_bot عبر PostgREST (upsert wa.contacts)      #
# --------------------------------------------------------------------------- #
def _postgrest_upsert_contact(cfg: dict, *, tenant_id: str, phone_intl: str,
                              name: str, timeout: float = 20.0) -> dict:
    """
    upsert في wa.contacts عبر PostgREST مع marketing_opt_in=true.
    idempotent على القيد الفريد (tenant_id, phone) — مرآة wa_contacts_tenant_phone_uniq.
    يتطلّب أن يكون schema «wa» مكشوفًا لـ PostgREST (Settings → API → Exposed schemas).
    إن لم يكن مكشوفًا، استخدم RPC داخل عامل Node بدل هذا المسار المباشر.
    """
    endpoint = f"{cfg['url']}/rest/v1/contacts"
    payload = json.dumps({
        "tenant_id": tenant_id,
        "phone": phone_intl,
        "name": name or None,
        "marketing_opt_in": True,
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        endpoint, data=payload, method="POST",
        headers={
            "apikey": cfg["key"],
            "Authorization": f"Bearer {cfg['key']}",
            "Content-Type": "application/json",
            "Content-Profile": "wa",          # الكتابة في schema wa
            "Accept-Profile": "wa",
            "Prefer": "resolution=merge-duplicates,return=representation",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        try:
            data = json.loads(body) if body else []
        except Exception:
            data = []
        return {"ok": True, "rows": data}


def deliver_optin_contact(prospect: dict, *, mode: str | None = None) -> dict:
    """
    ينفّذ التسليم الفعليّ لعميل «وافق» إلى whats_bot كجهة اتصال للمستأجر.

    السلوك:
      - sandbox (افتراضي): لا اتصال شبكيّ — يُسجّل النيّة ويُعيد action='logged_intent' (stub واضح).
      - live + إعداد Supabase مكتمل: upsert في wa.contacts مع marketing_opt_in=true (الخيار 1).
      - live بلا إعداد Supabase: stub آمن — يشرح أنّ نقطة الربط الفعليّة تتطلّب القاعدة/RPC أو ويبهوك.

    لا يُرسل رسالة بنفسه (التسليم ≠ إرسال)؛ بعد الإدخال يتولّى محرّك حملات whats_bot
    الإرسال ضمن ضوابط opt-in/quiet-hours/frequency-cap.
    """
    m = get_mode(mode)
    p = normalize_prospect(prospect)

    if not p["phone_intl"]:
        return {"ok": False, "action": "rejected",
                "note": "رقم غير صالح/مفقود — لا يمكن التسليم بلا هاتف قابل للتطبيع."}
    if not p["whatsappable"]:
        return {"ok": False, "action": "rejected",
                "note": "الرقم غير قابل للواتساب وفق قواعد leads.whatsappable."}
    if not p["consent"]:
        # فشل-مغلق على الموافقة: لا نُدخل جهة بلا opt-in صريح.
        return {"ok": False, "action": "rejected",
                "note": "لا توجد موافقة صريحة (consent=false) — opt-in إلزاميّ قبل التسليم."}

    cfg = supabase_config()
    tenant_id = p["tenant_id"] or (cfg["tenant_id"] if cfg else None)

    # وضع تجريبي: لا شبكة — نسجّل النيّة ونشرح نقطة الربط.
    if m != "live":
        logger.info("[sandbox] handoff intent → whats_bot | name=%s phone=%s wa_base=%s db=%s",
                    p["name"] or "—", _mask(p["phone_e164"]), wa_base_url(),
                    "configured" if cfg else "absent")
        return {
            "ok": True,
            "action": "logged_intent",
            "dry_run": True,
            "note": ("وضع تجريبي: سُجّلت نيّة التسليم بلا اتصال. في live مع ضبط "
                     "WHATSBOT_SUPABASE_URL/SERVICE_ROLE_KEY/TENANT_ID يتمّ upsert في wa.contacts "
                     "(marketing_opt_in=true). وإلا فنقطة الربط الفعليّة عبر القاعدة/RPC أو ويبهوك."),
            "prospect": {"name": p["name"], "phone_e164": p["phone_e164"],
                         "phone_intl": p["phone_intl"]},
            "tenant_id": tenant_id,
            "integration_option": 1,
        }

    # وضع live: نحتاج إعدادًا حقيقيًّا (فشل-مغلق).
    if not cfg:
        return {
            "ok": False,
            "action": "stub_no_backend",
            "note": ("live بلا قاعدة whats_bot: لم تُضبط WHATSBOT_SUPABASE_URL + "
                     "WHATSBOT_SUPABASE_SERVICE_ROLE_KEY + WHATSBOT_TENANT_ID. نقطة الربط الموصى بها "
                     "هي upsert في wa.contacts عبر PostgREST/RPC (schema wa مكشوف)، أو بناء ويبهوك "
                     "تكامل في whats_bot. تجنّب الإرسال المباشر عبر WaSenderAPI لأنّه يتجاوز الضوابط."),
            "integration_option": 1,
        }

    try:
        res = _postgrest_upsert_contact(
            cfg, tenant_id=tenant_id, phone_intl=p["phone_intl"], name=p["name"])
        logger.info("[live] handoff upsert → wa.contacts | tenant=%s phone=%s rows=%d",
                    _mask(tenant_id), _mask(p["phone_e164"]), len(res.get("rows", [])))
        return {
            "ok": True,
            "action": "upserted_contact",
            "note": "تمّ upsert جهة الاتصال في wa.contacts مع marketing_opt_in=true (idempotent على tenant+phone).",
            "tenant_id": tenant_id,
            "rows": res.get("rows", []),
            "integration_option": 1,
        }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
        return {"ok": False, "action": "error",
                "note": f"فشل PostgREST upsert (HTTP {e.code}). تحقّق من كشف schema wa والصلاحيات: {body[:200]}",
                "integration_option": 1}
    except Exception as e:  # شبكة/مهلة
        return {"ok": False, "action": "error",
                "note": f"خطأ شبكيّ أثناء التسليم: {e}", "integration_option": 1}


# --------------------------------------------------------------------------- #
# الواجهة العامّة                                                              #
# --------------------------------------------------------------------------- #
def handoff_prospect(prospect: dict, *, mode: str | None = None) -> dict:
    """
    العقد العامّ: سلّم عميلاً «وافق» إلى whats_bot.
    يُعيد دائمًا dict فيه على الأقل: {ok: bool, action: str, note: str}.
    آمن في كل الأحوال — لا اتصال شبكيّ في sandbox، وفشل-مغلق عند نقص الإعداد في live.
    """
    try:
        return deliver_optin_contact(prospect, mode=mode)
    except Exception as e:  # حارس أخير — لا نرمي للمتّصل أبدًا
        logger.exception("handoff_prospect غير متوقّع")
        return {"ok": False, "action": "error", "note": f"استثناء غير متوقّع: {e}"}


# --------------------------------------------------------------------------- #
# اختبار ذاتيّ يثبت العقد (بلا اتصال شبكيّ)                                     #
# --------------------------------------------------------------------------- #
def _self_test() -> int:
    failures = 0

    def check(cond: bool, label: str) -> None:
        nonlocal failures
        status = "PASS" if cond else "FAIL"
        if not cond:
            failures += 1
        print(f"  [{status}] {label}")

    print("== whatsbot_bridge :: اختبار ذاتيّ (وضع تجريبي، بلا شبكة) ==")

    # 1) عميل صالح موافق — في sandbox يجب أن يُسجّل النيّة دون شبكة.
    sample = {"name": "صالون النخبة", "phone": "0551234567", "consent": True}
    r1 = handoff_prospect(sample, mode="sandbox")
    print("  نتيجة (موافق/sandbox):", json.dumps(r1, ensure_ascii=False))
    check(isinstance(r1, dict), "النتيجة dict")
    check(r1.get("ok") is True, "ok=True لعميل موافق صالح")
    check(r1.get("action") == "logged_intent", "action=logged_intent في sandbox")
    check(r1.get("dry_run") is True, "dry_run=True (لا شبكة)")
    check(r1.get("prospect", {}).get("phone_e164") == "+966551234567", "تطبيع E.164 صحيح")
    check("note" in r1 and bool(r1["note"]), "note موجود")

    # 2) بلا موافقة → رفض (opt-in إلزاميّ).
    r2 = handoff_prospect({"name": "x", "phone": "0551112222", "consent": False}, mode="sandbox")
    check(r2.get("ok") is False and r2.get("action") == "rejected", "رفض عند consent=false")

    # 3) رقم غير صالح → رفض.
    r3 = handoff_prospect({"name": "x", "phone": "", "consent": True}, mode="sandbox")
    check(r3.get("ok") is False and r3.get("action") == "rejected", "رفض عند رقم مفقود")

    # 4) رقم أرضي غير قابل للواتساب → رفض.
    r4 = handoff_prospect({"name": "x", "phone": "0112345678", "consent": True}, mode="sandbox")
    check(r4.get("ok") is False and r4.get("action") == "rejected", "رفض رقم غير قابل للواتساب")

    # 5) live بلا إعداد Supabase → stub آمن (لا استثناء، لا شبكة فعليّة لأن cfg=None).
    saved = {k: os.environ.pop(k, None) for k in
             ("WHATSBOT_SUPABASE_URL", "WHATSBOT_SUPABASE_SERVICE_ROLE_KEY", "WHATSBOT_TENANT_ID")}
    try:
        r5 = handoff_prospect(sample, mode="live")
        check(r5.get("ok") is False and r5.get("action") == "stub_no_backend",
              "live بلا قاعدة → stub_no_backend آمن")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    # 6) المتانة: مدخل غير dict لا يرمي.
    r6 = handoff_prospect(None, mode="sandbox")  # type: ignore[arg-type]
    check(isinstance(r6, dict) and r6.get("ok") is False, "مدخل None لا يرمي ويُرفض بأمان")

    print(f"== انتهى الاختبار الذاتيّ: {'نجاح كامل' if failures == 0 else str(failures) + ' فشل'} ==")
    return 1 if failures else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    sys.exit(_self_test())
