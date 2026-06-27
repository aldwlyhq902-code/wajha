"""
mailer.py — قناة البريد الإلكترونيّ (SMTP) لإرسال العروض/التقارير.
=============================================================================
وحدة خفيفة (stdlib smtplib) لإرسال بريد تسويقيّ فرديّ. تُقرأ الإعدادات من البيئة،
وتعمل في «وضع تجريبيّ» تلقائيًّا إن لم تُضبط (لا تفشل، تُعيد dry_run=True).

متغيّرات البيئة:
    BOOKING_SMTP_HOST     مضيف SMTP (مثال smtp.gmail.com) — مطلوب للإرسال الفعليّ
    BOOKING_SMTP_PORT     منفذ (افتراضي 587)
    BOOKING_SMTP_USER     اسم المستخدم
    BOOKING_SMTP_PASS     كلمة المرور/مفتاح التطبيق
    BOOKING_SMTP_FROM     عنوان المُرسِل (افتراضي = USER)
    BOOKING_SMTP_TLS      "1"/"0" تفعيل STARTTLS (افتراضي 1)

العقد العام:
    is_configured() -> bool
    send_email(to, subject, html=None, text=None) -> dict {ok, dry_run, error}
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def smtp_config() -> dict:
    host = _env("BOOKING_SMTP_HOST")
    user = _env("BOOKING_SMTP_USER")
    return {
        "host": host,
        "port": int(_env("BOOKING_SMTP_PORT", "587") or 587),
        "user": user,
        "password": _env("BOOKING_SMTP_PASS"),
        "from": _env("BOOKING_SMTP_FROM", user or "no-reply@example.com"),
        "tls": _env("BOOKING_SMTP_TLS", "1") not in ("0", "false", "no"),
    }


def is_configured() -> bool:
    cfg = smtp_config()
    return bool(cfg["host"] and cfg["from"])


def _valid_email(addr: str) -> bool:
    addr = (addr or "").strip()
    return bool(addr) and "@" in addr and "." in addr.split("@")[-1]


def send_email(to: str, subject: str, html: str | None = None,
               text: str | None = None, timeout: int = 25) -> dict:
    """يرسل بريداً فرديًّا. يُعيد {ok, dry_run, error}.

    إن لم يُضبط SMTP أو كان العنوان غير صالح → dry_run/أو خطأ واضح دون رمي استثناء.
    """
    if not _valid_email(to):
        return {"ok": False, "error": "عنوان بريد غير صالح", "to": to}

    cfg = smtp_config()
    if not (cfg["host"] and cfg["from"]):
        # غير مُعدّ → تجريبيّ (لا فشل)
        return {"ok": True, "dry_run": True, "to": to,
                "note": "SMTP غير مُعدّ — لم يُرسَل فعليًّا (اضبط BOOKING_SMTP_*)."}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = to
    if text:
        msg.attach(MIMEText(text, "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))
    if not (text or html):
        msg.attach(MIMEText("", "plain", "utf-8"))

    try:
        if cfg["port"] == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=timeout, context=ctx) as s:
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.sendmail(cfg["from"], [to], msg.as_string())
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=timeout) as s:
                if cfg["tls"]:
                    s.starttls(context=ssl.create_default_context())
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.sendmail(cfg["from"], [to], msg.as_string())
        return {"ok": True, "dry_run": False, "to": to}
    except Exception as e:
        return {"ok": False, "error": f"فشل الإرسال: {e}", "to": to}


# تحويل نصّ بسيط إلى HTML عربيّ RTL (مع بكسل تتبّع اختياري)
def text_to_html(text: str, pixel_url: str = "", cta_url: str = "", cta_label: str = "") -> str:
    body = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    cta = (f'<p style="text-align:center;margin:22px 0"><a href="{cta_url}" '
           f'style="background:#25D366;color:#053d2b;text-decoration:none;font-weight:800;'
           f'padding:13px 28px;border-radius:12px;display:inline-block">{cta_label or "اطّلع الآن"}</a></p>'
           if cta_url else "")
    pixel = f'<img src="{pixel_url}" width="1" height="1" alt="" style="display:none">' if pixel_url else ""
    return (
        '<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8"></head>'
        '<body style="font-family:Tahoma,Arial,sans-serif;background:#f1f5f9;margin:0;padding:24px">'
        '<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:16px;padding:26px;'
        'color:#0f172a;line-height:1.8;font-size:15px">'
        f'<p>{body}</p>{cta}'
        '<hr style="border:0;border-top:1px solid #e2e8f0;margin:20px 0">'
        '<p style="font-size:12px;color:#94a3b8">للتوقّف عن الرسائل، ردّوا بكلمة «إيقاف».</p>'
        f'</div>{pixel}</body></html>'
    )


if __name__ == "__main__":
    print("SMTP مُعدّ؟", is_configured())
    r = send_email("test@example.com", "اختبار", text="مرحباً، هذه رسالة اختبار.")
    print("نتيجة:", r)
    assert isinstance(r, dict) and ("ok" in r)
    bad = send_email("not-an-email", "x", text="y")
    assert bad["ok"] is False
    html = text_to_html("سطر١\nسطر٢", pixel_url="https://x/p.gif", cta_url="https://x/r", cta_label="التقرير")
    assert "سطر١" in html and "p.gif" in html and "التقرير" in html
    print("PASS: mailer.py")
