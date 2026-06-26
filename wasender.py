"""
عميل WaSenderAPI لإرسال رسائل واتساب تلقائياً (بدون اعتماديات — urllib القياسية).
=============================================================================
الـ API:  POST https://www.wasenderapi.com/api/send-message
الترويسة: Authorization: Bearer <API_KEY>
الجسم:    {"to": "+9665XXXXXXXX", "text": "..."}   (الرقم بصيغة E.164)
الرد:     {"success": true, "data": {"msgId": ..., "jid": ..., "status": "in_progress"}}

المفتاح يُقرأ من متغيّر البيئة WASENDER_API_KEY (لا تكتبه في الكود).
    PowerShell:  $env:WASENDER_API_KEY = "ضع_مفتاحك"
    أو مرّره عبر api_key=...

ميزات أمان: تحديد معدّل الإرسال، إعادة المحاولة مع backoff عند 429، ووضع تجريبي (dry_run).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from leads import normalize_phone

logger = logging.getLogger("wasender")

SEND_URL = "https://www.wasenderapi.com/api/send-message"
STATUS_URL = "https://www.wasenderapi.com/api/status"

# أماكن قراءة مفتاح الـ API (بالترتيب): الوسيط → متغيّر البيئة → ملف مفتاح
KEY_FILES = ("wasender.key", "booking_data/.wasender_key")


def load_api_key(explicit: str | None = None) -> str | None:
    """يقرأ مفتاح WaSenderAPI من: الوسيط، ثم WASENDER_API_KEY، ثم ملف مفتاح."""
    if explicit:
        return explicit.strip()
    env = os.environ.get("WASENDER_API_KEY")
    if env and env.strip():
        return env.strip()
    for fp in KEY_FILES:
        p = Path(fp)
        if p.exists():
            val = p.read_text(encoding="utf-8").strip()
            if val:
                return val
    return None


def to_e164(phone: str, country: str = "966") -> str | None:
    """حوّل أي رقم إلى صيغة E.164 مع علامة + (المطلوبة من WaSenderAPI)."""
    if not phone:
        return None
    if str(phone).strip().startswith("+"):
        digits = "".join(ch for ch in str(phone) if ch.isdigit())
        return "+" + digits if digits else None
    intl = normalize_phone(phone, country)
    return ("+" + intl) if intl else None


class WaSenderClient:
    def __init__(self, api_key: str | None = None, *, min_interval: float = 4.0,
                 dry_run: bool = False, max_retries: int = 3,
                 country: str = "966", send_url: str = SEND_URL):
        self.api_key = load_api_key(api_key)
        self.min_interval = max(0.0, float(min_interval))
        self.dry_run = dry_run
        self.max_retries = max_retries
        self.country = country
        self.send_url = send_url
        self._last_sent = 0.0
        if not self.api_key and not self.dry_run:
            raise RuntimeError(
                "مفتاح WaSenderAPI غير موجود. اختر إحدى الطرق:\n"
                "  • متغيّر بيئة:  $env:WASENDER_API_KEY = \"مفتاحك\"\n"
                "  • أو ملف: ضع المفتاح في  wasender.key  بمجلد المشروع\n"
                "  • أو مرّر api_key=… (أو استخدم dry_run=True للتجربة)."
            )

    # ---- داخلي ------------------------------------------------------------ #
    def _throttle(self) -> None:
        wait = self.min_interval - (time.time() - self._last_sent)
        if wait > 0:
            time.sleep(wait)
        self._last_sent = time.time()

    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_err = "غير معروف"
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(
                self.send_url, data=data, method="POST",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                d = body.get("data", {}) if isinstance(body, dict) else {}
                return {"ok": bool(body.get("success", True)),
                        "msg_id": d.get("msgId"), "status": d.get("status"), "raw": body}
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code == 429 and attempt < self.max_retries:
                    backoff = self.min_interval * (2 ** attempt) or 2 ** attempt
                    logger.warning("429 (تجاوز المعدّل) — انتظار %.1fث", backoff)
                    time.sleep(backoff)
                    last_err = "HTTP 429"
                    continue
                return {"ok": False, "error": f"HTTP {e.code}: {body[:200]}"}
            except Exception as e:  # شبكة/مهلة
                last_err = str(e)
                if attempt < self.max_retries:
                    time.sleep(self.min_interval * attempt + 1)
                    continue
        return {"ok": False, "error": f"فشل بعد {self.max_retries} محاولات: {last_err}"}

    # ---- عام -------------------------------------------------------------- #
    def send_text(self, to: str, text: str) -> dict:
        e164 = to if str(to).startswith("+") else to_e164(to, self.country)
        if not e164:
            return {"ok": False, "error": "رقم غير صالح", "to": to}
        if self.dry_run:
            logger.info("[تجريبي] → %s : %s", e164, (text or "")[:50].replace("\n", " "))
            return {"ok": True, "dry_run": True, "to": e164}
        res = self._post({"to": e164, "text": text})
        res["to"] = e164
        return res

    def send_image(self, to: str, image_url: str, caption: str = "") -> dict:
        """إرسال صورة برابط (أفضل جهد — حقل imageUrl على نفس الـ endpoint)."""
        e164 = to if str(to).startswith("+") else to_e164(to, self.country)
        if not e164:
            return {"ok": False, "error": "رقم غير صالح", "to": to}
        if self.dry_run:
            logger.info("[تجريبي صورة] → %s : %s", e164, image_url[:50])
            return {"ok": True, "dry_run": True, "to": e164}
        payload = {"to": e164, "imageUrl": image_url}
        if caption:
            payload["text"] = caption
        res = self._post(payload)
        res["to"] = e164
        return res

    def status(self) -> dict:
        if not self.api_key:
            return {"ok": False, "error": "لا يوجد مفتاح"}
        req = urllib.request.Request(
            STATUS_URL, headers={"Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return {"ok": True, "raw": json.loads(resp.read().decode("utf-8"))}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# اختبار سريع للاتصال: يرسل رسالة لرقمك أنت للتأكد من المفتاح
if __name__ == "__main__":
    import argparse, sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="اختبار إرسال WaSenderAPI")
    ap.add_argument("--to", required=True, help="رقمك للاختبار (مثال 05xxxxxxxx)")
    ap.add_argument("--text", default="رسالة اختبار من نظام الحجز ✅")
    ap.add_argument("--country", default="966")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    c = WaSenderClient(dry_run=a.dry_run, country=a.country, min_interval=0)
    print(c.send_text(a.to, a.text))
