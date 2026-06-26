"""
نقطة دخول الإنتاج (WSGI) لنظام الحجز.
=====================================
يُستخدم مع خادم إنتاج مثل gunicorn على Render:

    gunicorn wsgi:app

يُهيّئ قاعدة البيانات (إنشاء/ترحيل) قبل بدء الخدمة.
"""

from booking_system import app, init_db

init_db()

# للتشغيل المحلي السريع بدون gunicorn:  python wsgi.py
if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5001")))
