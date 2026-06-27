"""
خادم ويب لسحب بيانات Google Maps.
يوفّر واجهة عربية في المتصفح تعتمد على scraper.py.

التشغيل:
    python app.py
ثم افتح: http://localhost:5000
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# على Windows، الطرفية cp1252 تُسقط البرنامج عند طباعة العربية → UnicodeEncodeError.
# نُجبر UTF-8 قبل إعداد السجلات أدناه.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from flask import Flask, render_template, request, jsonify, send_file

from scraper import GoogleMapsScraper, Place, save_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gmaps_web")

app = Flask(__name__, static_folder="static", template_folder="templates")

# مخزن المهام في الذاكرة (يكفي للاستخدام المحلي)
from collections import OrderedDict

JOBS: "OrderedDict[str, dict]" = OrderedDict()
_JOBS_LOCK = threading.Lock()
MAX_JOBS = 200  # حدّ أقصى لعدد المهام المحفوظة (تجنّب تسرّب الذاكرة)
# سمح بمهمتين متزامنتين كحدّ أقصى (كل مهمة تشغّل متصفح Chromium كامل)
_RUN_SEMAPHORE = threading.Semaphore(2)


def _evict_old_jobs() -> None:
    """احذف أقدم المهام المنتهية إذا تجاوز العدد الحدّ."""
    with _JOBS_LOCK:
        while len(JOBS) > MAX_JOBS:
            for jid, j in list(JOBS.items()):
                if j.get("status") in ("done", "error"):
                    JOBS.pop(jid, None)
                    break
            else:
                break  # لا توجد مهمة منتهية يمكن حذفها


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _place_to_dict(p: Place) -> dict:
    d = asdict(p)
    # تنقيح القيم النصية
    for k, v in d.items():
        if isinstance(v, str):
            d[k] = v.strip()
    return d


def _parse_proxy(text: str | None) -> dict | None:
    if not text:
        return None
    parts = [p.strip() for p in text.split(";") if p.strip()]
    proxy = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            proxy[k.strip()] = v.strip()
        else:
            proxy.setdefault("server", p)
    return proxy or None


# --------------------------------------------------------------------------- #
# صفحة الواجهة                                                                #
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------- #
# بدء مهمة بحث (في خيط خلفي)                                                  #
# --------------------------------------------------------------------------- #
def _run_search_job(job_id: str, mode: str, params: dict) -> None:
    """تشغيل الساحب فعلياً في خيط خلفي وتحديث حالة المهمة."""
    job = JOBS[job_id]

    def progress_cb(current: int, total: int) -> None:
        job["progress"]["current"] = current
        if total:
            job["progress"]["total"] = total

    # حدّ التزامن: ابقَ في حالة «queued» حتى يتوفّر متصفح
    with _RUN_SEMAPHORE:
        job["status"] = "running"
        job["started_at"] = datetime.now().isoformat()

        headless = params.get("headless", True)
        lang = params.get("lang", "ar")
        proxy = _parse_proxy(params.get("proxy"))

        scraper = GoogleMapsScraper(headless=headless, lang=lang, proxy=proxy)
        try:
            with scraper:
                if mode == "search":
                    keyword = params.get("keyword", "")
                    city = params.get("city", "")
                    max_results = params.get("max", 20)
                    results = scraper.search(keyword, city, max_results,
                                             on_progress=progress_cb)
                elif mode == "url":
                    url = params.get("url", "")
                    results = [scraper.extract_url(url)]
                elif mode == "list":
                    urls = [u.strip() for u in params.get("urls", "").splitlines() if u.strip()]
                    results = scraper.extract_urls(urls, on_progress=progress_cb)
                else:
                    results = []

            job["results"] = [_place_to_dict(p) for p in results]
            job["status"] = "done"
            # حافظ على المجموع الحقيقي ولا تُصفّره
            job["progress"]["current"] = len(results)
            if not job["progress"].get("total"):
                job["progress"]["total"] = len(results)
            logger.info("Job %s done: %d results", job_id, len(results))
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
            logger.exception("Job %s failed", job_id)
        finally:
            job["finished_at"] = datetime.now().isoformat()


@app.route("/api/search", methods=["POST"])
def api_search():
    """ابدأ مهمة سحب. يُرجع job_id فوراً."""
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "بيانات غير صالحة"}), 400
    mode = data.get("mode", "search")
    if mode not in ("search", "url", "list"):
        return jsonify({"error": "وضع غير صالح"}), 400
    if mode == "search" and not data.get("keyword"):
        return jsonify({"error": "أدخل كلمة البحث"}), 400
    if mode == "url" and not data.get("url"):
        return jsonify({"error": "أدخل الرابط"}), 400
    if mode == "list" and not data.get("urls"):
        return jsonify({"error": "الصق قائمة الروابط/الأسماء"}), 400

    # تحقّق وقيّد عدد النتائج (1..100) لتجنّب الانهيار والأحمال الضخمة
    try:
        max_results = max(1, min(int(data.get("max", 20)), 100))
    except (TypeError, ValueError):
        return jsonify({"error": "قيمة العدد غير صالحة"}), 400
    data["max"] = max_results

    # احسب المجموع الأولي حسب الوضع
    if mode == "list":
        total = len([u for u in data.get("urls", "").splitlines() if u.strip()])
    elif mode == "url":
        total = 1
    else:
        total = max_results

    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "mode": mode,
            "results": [],
            "error": None,
            "progress": {"current": 0, "total": total},
            "params": data,
        }
    _evict_old_jobs()

    thread = threading.Thread(
        target=_run_search_job, args=(job_id, mode, data), daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id: str):
    """تحقّق من حالة المهمة (يستخدمها المتصفح للتصويت)."""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "مهمة غير موجودة"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "error": job["error"],
        "results": job["results"] if job["status"] == "done" else [],
        "count": len(job["results"]),
    })


# --------------------------------------------------------------------------- #
# حفظ النتائج مباشرةً في القاعدة السحابية (نظام الحجز) — وسيط لتفادي CORS       #
# --------------------------------------------------------------------------- #
@app.route("/api/save_to_cloud", methods=["POST"])
def save_to_cloud():
    body = request.get_json(force=True, silent=True) or {}
    results = body.get("results") or []
    cloud = (body.get("cloud_url") or os.environ.get("BOOKING_CLOUD_URL") or "").strip().rstrip("/")
    key = (body.get("owner_key") or os.environ.get("BOOKING_OWNER_PASSWORD") or "").strip()
    if not cloud:
        return jsonify({"error": "حدّد رابط النظام السحابي"}), 400
    if not key:
        return jsonify({"error": "حدّد كلمة مرور المالك"}), 400
    if not results:
        return jsonify({"error": "لا توجد نتائج للحفظ"}), 400
    req = urllib.request.Request(
        cloud + "/api/owner/import",
        data=json.dumps(results, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Owner-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return jsonify(json.loads(r.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        msg = "كلمة مرور المالك غير صحيحة" if e.code == 403 else f"رفضت السحابة ({e.code}): {detail}"
        return jsonify({"error": msg}), 502
    except Exception as e:
        return jsonify({"error": f"تعذّر الاتصال بالسحابة: {e}"}), 502


# --------------------------------------------------------------------------- #
# الركض                                                                        #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    print("=" * 55)
    print(" أداة سحب Google Maps — واجهة الويب")
    print(" افتح المتصفح على: http://localhost:5000")
    print("=" * 55)
    app.run(host="127.0.0.1", port=5000, debug=False)
