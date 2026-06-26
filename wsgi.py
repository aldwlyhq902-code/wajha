"""
نقطة دخول الإنتاج (WSGI) لنظام الحجز.
=====================================
يُستخدم مع خادم إنتاج مثل gunicorn على Render:

    gunicorn wsgi:app

يُهيّئ قاعدة البيانات (إنشاء/ترحيل) قبل بدء الخدمة.
"""

import logging
from booking_system import app, init_db

# لا نُسقط الخدمة إن فشل تهيئة القاعدة (مثلاً خطأ اتصال Turso) —
# تبقى /api/health قادرة على إظهار الخطأ الدقيق للتشخيص.
try:
    init_db()
except Exception as e:
    logging.getLogger("booking").error("init_db failed at boot: %s", e)

# للتشغيل المحلي السريع بدون gunicorn:  python wsgi.py
if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5001")))
