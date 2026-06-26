"""
وحدة سحب البيانات من Google Maps باستخدام Playwright.

النمط الصحيح للسحب:
    بحث  →  التمرير لجمع النتائج  →  النقر على كل نتيجة  →  استخراج الحقول
    من لوحة التفاصيل (التي تظهر فقط بعد النقر، عبر سمات data-item-id).

تحذير قانوني:
    الكشط الآلي لـ Google Maps قد يخلف شروط خدمة Google.
    استخدم الأداة لأغراض البحث الشخصي والتعليمي، وتحمّل مسؤولية استخدامك.
    للاستخدام التجاري استخدم Google Places API الرسمي.

الحقول المُصدّرة لكل موقع:
    name, address, category, rating, reviews_count, phone, website,
    plus_code, price_range, opening_hours, is_open_now,
    latitude, longitude, place_url, image_url, description, timestamp
"""

from __future__ import annotations

import csv
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

logger = logging.getLogger("gmaps_scraper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------- #
@dataclass
class Place:
    name: str = ""
    address: str = ""
    category: str = ""
    rating: float | None = None
    reviews_count: int | None = None
    phone: str = ""
    website: str = ""
    plus_code: str = ""
    price_range: str = ""
    opening_hours: list[str] = field(default_factory=list)
    is_open_now: str = ""
    latitude: float | None = None
    longitude: float | None = None
    place_url: str = ""
    image_url: str = ""
    description: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
def _to_float(text: str | None) -> float | None:
    if not text:
        return None
    # خُذ أول عدد عشري
    m = re.search(r"-?\d+[.,]?\d*", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _to_int(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    try:
        return int(digits) if digits else None
    except ValueError:
        return None


def _clean(text: str) -> str:
    """تنظيف القيم: إزالة الأسطر الجديدة الزائدة والمسافات ورموز بادئة."""
    if not text:
        return ""
    # حوّل الأسطر/الجدولة إلى مسافات حتى لا تُفقد عند حذف محارف التحكم
    text = text.replace("\n", " ").replace("\t", " ").replace("\r", " ")
    # أزل كل المحارف غير المرئية: Cc (تحكم) + Cf (تنسيق/bidi/ZWSP/BOM) + Co (أيقونات PUA)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    # دمج المسافات المتعددة
    text = re.sub(r"\s+", " ", text).strip()
    # إزالة رموز عشوائية شائعة في البداية
    text = re.sub(r"^[\s\u200f\u200e\u202a-\u202e\u2013\-•·]+", "", text)
    return text.strip()


def _coords_from_url(url: str) -> tuple[float | None, float | None]:
    try:
        m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url)
        if m:
            return float(m.group(1)), float(m.group(2))
        m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
        if m:
            return float(m.group(1)), float(m.group(2))
    except Exception:
        pass
    return None, None


# --------------------------------------------------------------------------- #
class GoogleMapsScraper:
    def __init__(
        self,
        headless: bool = True,
        slow_mo: int = 30,
        proxy: dict | None = None,
        lang: str = "ar",
        timeout: int = 20000,
    ):
        self.headless = headless
        self.slow_mo = slow_mo
        self.proxy = proxy
        self.lang = lang
        self.timeout = timeout
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # ---- lifecycle -------------------------------------------------------- #
    def __enter__(self) -> "GoogleMapsScraper":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless, slow_mo=self.slow_mo
        )
        self._context = self._browser.new_context(
            user_agent=USER_AGENT,
            locale=self.lang,
            viewport={"width": 1366, "height": 850},
            proxy=self.proxy,
        )
        self._context.set_default_timeout(self.timeout)
        self._page = self._context.new_page()
        logger.info("Browser started.")

    def close(self) -> None:
        # كل مورد في try مستقل حتى لا يمنع فشلُ أحدها إغلاق البقية (تجنّب تسرّب المتصفح).
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                logger.warning("Failed to close browser context.", exc_info=True)
            finally:
                self._context = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                logger.warning("Failed to close browser.", exc_info=True)
            finally:
                self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                logger.warning("Failed to stop Playwright driver.", exc_info=True)
            finally:
                self._playwright = None
        self._page = None
        logger.info("Browser closed.")

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Scraper not started.")
        return self._page

    # ---- navigation ------------------------------------------------------- #
    def _with_hl(self, url: str) -> str:
        """أضف معامل hl لضمان لغة واجهة ثابتة (locale وحده لا يكفي مع Google Maps)."""
        if "hl=" in url:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}hl={self.lang}"

    def _goto_search(self, query: str) -> None:
        page = self.page
        url = f"https://www.google.com/maps/search/{quote_plus(query)}/?hl={self.lang}"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        self._dismiss_consent()

    def _dismiss_consent(self) -> None:
        page = self.page
        for label in ["Accept all", "موافق على الكل", "Accept", "Agree", "I agree", "Got it"]:
            try:
                btn = page.get_by_role("button", name=label, exact=False)
                if btn.count() > 0:
                    btn.first.click(timeout=2000)
                    break
            except Exception:
                continue

    # ---- scroll & collect result links ------------------------------------ #
    def _scroll_results(self, max_results: int) -> list[str]:
        """تمرير قائمة النتائج وجمع روابط كل مكان."""
        page = self.page
        feed_selector = 'div[role="feed"]'
        try:
            page.wait_for_selector(feed_selector, timeout=10000)
        except PlaywrightTimeoutError:
            logger.warning("Could not find the results feed.")
            return []

        collected: list[str] = []
        seen: set[str] = set()
        last_count = 0
        stable_rounds = 0
        max_rounds = 60

        while len(collected) < max_results and max_rounds > 0:
            anchors = page.locator(f'{feed_selector} a[href*="/maps/place/"]')
            count = anchors.count()
            for i in range(count):
                try:
                    href = anchors.nth(i).get_attribute("href")
                    if href and href not in seen and "/maps/place/" in href:
                        seen.add(href)
                        collected.append(href)
                        if len(collected) >= max_results:
                            break
                except Exception:
                    continue

            # التمرير للأسفل
            try:
                page.locator(feed_selector).first.evaluate(
                    "el => el.scrollBy(0, el.scrollHeight)"
                )
            except Exception:
                break

            page.wait_for_timeout(1300)
            max_rounds -= 1

            # كشف الاستقرار (لم تعد تظهر نتائج جديدة)
            if count == last_count:
                stable_rounds += 1
                if stable_rounds >= 4:
                    break
            else:
                stable_rounds = 0
            last_count = count

        logger.info("Collected %d result links.", len(collected))
        return collected[:max_results]

    # ---- extract a single place (by clicking its result row) ------------- #
    @staticmethod
    def _feature_id(url: str) -> str:
        """المعرّف الثابت للمكان داخل الرابط (0x...:0x...). يصمد رغم تغيّر بقية الرابط."""
        m = re.search(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", url or "")
        return m.group(0) if m else ""

    def _click_result(self, place_url: str) -> bool:
        """انقر على بطاقة النتيجة المطابقة لـ place_url لفتح لوحة التفاصيل.

        ملاحظة مهمة: لا نلجأ أبداً إلى النقر على «أول» نتيجة عند فشل المطابقة،
        لأن ذلك كان يستخرج المكان الأول مراراً ويُنتج صفوفاً مكررة/خاطئة.
        عند تعذّر التطابق الدقيق نعيد False ليتولّى الفتح المباشر للرابط.
        """
        page = self.page
        # 1) تطابق دقيق على الـ href
        card = page.locator(f'div[role="feed"] a[href="{place_url}"]')
        # 2) تطابق على المعرّف الثابت للمكان (أكثر متانة)
        if card.count() == 0:
            fid = self._feature_id(place_url)
            if fid:
                card = page.locator(f'div[role="feed"] a[href*="{fid}"]')
        try:
            if card.count() != 1:
                # صفر أو أكثر من بطاقة → غامض، لا نخمّن
                return False
            card.first.scroll_into_view_if_needed(timeout=3000)
            card.first.click(timeout=8000)
            page.wait_for_timeout(2500)
            # تحقّق أن اللوحة المفتوحة تخص المكان المطلوب فعلاً
            fid = self._feature_id(place_url)
            if fid and fid not in (page.url or ""):
                return False
            return True
        except Exception as e:
            logger.debug("click failed: %s", e)
            return False

    def _open_place_directly(self, place_url: str) -> bool:
        """بديل: افتح رابط المكان مباشرة ثم انتظر ظهور لوحة التفاصيل."""
        page = self.page
        page.goto(self._with_hl(place_url), wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        self._dismiss_consent()
        # انتظر ظهور حاوي التفاصيل (h1 أو data-item-id)
        for _ in range(8):
            try:
                if page.locator('h1.DUwDvf').count() > 0 and page.locator('h1.DUwDvf').first.inner_text(timeout=1000).strip():
                    return True
            except Exception:
                pass
            page.wait_for_timeout(700)
        return page.locator('h1.DUwDvf').count() > 0

    # ---- field extractors ------------------------------------------------- #
    def _field_by_item_id(self, iid: str) -> str:
        """اقرأ نص عنصر عبر data-item-id (قد يكون button أو a)."""
        page = self.page
        for tag in ("button", "a", "div"):
            try:
                el = page.locator(f'{tag}[data-item-id="{iid}"]').first
                if el.count() > 0:
                    return _clean(el.inner_text(timeout=2000))
            except Exception:
                continue
        return ""

    def _field_by_item_id_prefix(self, prefix: str) -> str:
        """اقرأ نص أول عنصر يبدأ data-item-id بهذا المقطع (مثل phone: )."""
        page = self.page
        for tag in ("button", "a", "div"):
            try:
                els = page.locator(f'{tag}[data-item-id^="{prefix}"]')
                if els.count() > 0:
                    return _clean(els.first.inner_text(timeout=2000))
            except Exception:
                continue
        return ""

    def _field_href_by_item_id(self, iid: str) -> str:
        """اقرأ href لرابط عبر data-item-id (مثل website = authority)."""
        page = self.page
        for tag in ("a", "button"):
            try:
                el = page.locator(f'{tag}[data-item-id="{iid}"]').first
                if el.count() > 0:
                    return el.get_attribute("href") or ""
            except Exception:
                continue
        return ""

    def _read_rating_and_reviews(self) -> tuple[float | None, int | None]:
        page = self.page
        rating = None
        reviews = None
        try:
            block = page.locator('div.F7nice').first
            if block.count() > 0:
                text = block.inner_text(timeout=2000)
                # النص كالتالي: "4.9\n(3,407)"
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                rating = _to_float(lines[0]) if lines else None
                reviews = _to_int(lines[1]) if len(lines) > 1 else None
        except Exception:
            pass
        return rating, reviews

    # أسماء الأيام (عربي + إنجليزي) لتمييز صفوف جدول الساعات عن صفوف الأسعار
    _DAY_NAMES = (
        "الأحد", "الإثنين", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت",
        "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
    )
    _STATUS_RE = re.compile(r"مفتوح|مغلق|يفتح|يغلق|Open|Closed|Opens|Closes|Closing|٢٤ ساعة|Open 24")

    def _read_opening_hours(self) -> tuple[list[str], str]:
        """اقرأ ساعات العمل وحالة الفتح.

        بنية Google Maps الحالية:
          - عنصر `[jsaction*="openhours"]` يحمل نص الحالة (مثل «مفتوح · يغلق ٩ م»)
            وبالنقر عليه يتوسّع جدول أيام الأسبوع.
          - زر `button[aria-label*="ساعات"]` غالباً مخفي فلا يصلح للنقر.
        """
        page = self.page
        hours: list[str] = []
        is_open = ""

        # ابحث عن عنصر الساعات الذي يحمل نص الحالة
        status_loc = page.locator('[jsaction*="openhours"]')
        status_el = None
        try:
            n = status_loc.count()
        except Exception:
            n = 0
        for i in range(n):
            try:
                txt = (status_loc.nth(i).inner_text(timeout=600) or "").strip()
            except Exception:
                continue
            if txt and self._STATUS_RE.search(txt):
                for line in txt.splitlines():
                    line = line.strip()
                    if line and self._STATUS_RE.search(line):
                        is_open = _clean(line)
                        break
                if not is_open:
                    is_open = _clean(txt.splitlines()[0])
                status_el = status_loc.nth(i)
                break

        # بديل: اقرأ الحالة من نص الصفحة المرئي
        if not is_open:
            try:
                body = page.inner_text("body", timeout=2000)
                m = re.search(r"(مفتوح[^\n|]*|مغلق[^\n|]*|Open[^\n|]*|Closed[^\n|]*)", body)
                if m:
                    is_open = _clean(m.group(0))
            except Exception:
                pass

        # المسار الأساسي: اقرأ ساعات كل يوم من سمة data-value لأزرار الأيام.
        # هذه السمة موجودة في DOM دون الحاجة لتوسيع الجدول → أكثر متانة وأسرع.
        seen: set[str] = set()
        try:
            day_btns = page.locator('[jsaction*="openhours"][data-value]')
            dn = day_btns.count()
            for i in range(dn):
                try:
                    dv = day_btns.nth(i).get_attribute("data-value") or ""
                except Exception:
                    continue
                dv = re.sub(r"\s+", " ", _clean(dv.replace("،", " "))).strip()
                if dv and any(day in dv for day in self._DAY_NAMES) and dv not in seen:
                    seen.add(dv)
                    hours.append(dv)
        except Exception:
            pass

        # بديل: إن لم نحصل على أيام كافية، وسّع الجدول بالنقر واقرأ صفوفه
        if len(hours) < 2:
            hours = []
            seen.clear()
            try:
                target = status_el if status_el is not None else (status_loc.first if n else None)
                if target is not None:
                    target.click(timeout=4000)
                    page.wait_for_timeout(1200)
            except Exception:
                pass
            try:
                rows = page.locator('table tr')
                rn = rows.count()
                for i in range(rn):
                    try:
                        t = (rows.nth(i).inner_text(timeout=500) or "").strip()
                    except Exception:
                        continue
                    if not t:
                        continue
                    if "US$" in t or "$" in t or "€" in t or "£" in t:
                        continue
                    t = re.sub(r"\s+", " ", _clean(t.replace("\t", " "))).strip()
                    if t and any(day in t for day in self._DAY_NAMES) and t not in seen:
                        seen.add(t)
                        hours.append(t)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
            except Exception:
                pass

        return hours, is_open

    def _read_image(self) -> str:
        page = self.page
        for sel in [
            'img[src*="googleusercontent"]',
            'img[src*="googleapis"]',
            'button[jsaction*="heroHeaderImage"] img',
        ]:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    src = el.get_attribute("src") or ""
                    if src.startswith("http"):
                        return src
            except Exception:
                continue
        return ""

    def _read_description(self) -> str:
        page = self.page
        # حاول فتح تبويب «نظرة عامة» / Overview لظهور الوصف الكامل
        for label in ["نظرة عامة", "Overview", "لمحة", "About"]:
            try:
                tab = page.get_by_role("tab", name=label, exact=False)
                if tab.count() > 0:
                    tab.first.click(timeout=3000)
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                continue
        for sel in ['div.PYvSYb', 'div[jsaction*="pane.description"]',
                    'div.bPhDSc', 'div[jslog*="description"]']:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    return (el.inner_text(timeout=2000) or "").strip()
            except Exception:
                continue
        return ""

    def _read_category(self) -> str:
        page = self.page
        for sel in [
            'button[jsaction*="pane.rating.category"]',
            'button[jsaction*="category"]',
        ]:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    return (el.inner_text(timeout=2000) or "").strip()
            except Exception:
                continue
        return ""

    def _read_price_range(self) -> str:
        """أسعار (عناصر tr بـ data-item-id رقمية: 0,1,2)."""
        page = self.page
        try:
            el = page.locator('tr[data-item-id="0"]').first
            if el.count() > 0:
                return (el.inner_text(timeout=1500) or "").strip()
        except Exception:
            pass
        return ""

    def _extract_current_place(self, place_url: str) -> Place:
        """استخراج بيانات المكان المعروض حالياً في لوحة التفاصيل."""
        page = self.page
        place = Place(place_url=place_url, timestamp=datetime.now().isoformat())

        # الاسم
        try:
            place.name = (page.locator('h1.DUwDvf').first.inner_text(timeout=3000) or "").strip()
        except Exception:
            pass

        place.category = self._read_category()
        place.address = self._field_by_item_id("address")
        place.phone = self._field_by_item_id_prefix("phone:")
        place.website = self._field_href_by_item_id("authority")
        if not place.website:
            place.website = self._field_by_item_id("authority")
        place.plus_code = self._field_by_item_id("oloc")
        if not place.plus_code:
            place.plus_code = self._field_by_item_id("plus_code")
        place.price_range = self._read_price_range()

        place.rating, place.reviews_count = self._read_rating_and_reviews()
        place.image_url = self._read_image()
        place.description = self._read_description()
        # تُقرأ الساعات أخيراً لأن توسيع الجدول يُعدّل لوحة التفاصيل
        place.opening_hours, place.is_open_now = self._read_opening_hours()

        lat, lng = _coords_from_url(page.url)
        place.latitude = lat
        place.longitude = lng

        return place

    def _extract_place_by_url(self, place_url: str) -> Place:
        """افتح المكان مباشرة ثم استخرج (يُستخدم لوضع url / file)."""
        self._open_place_directly(place_url)
        # قد لا تظهر لوحة التفاصيل عند فتح رابط مباشرة؛ نُحاول الاستخراج على أي حال
        place = self._extract_current_place(self.page.url)
        place.place_url = place_url
        return place

    # ---- public API ------------------------------------------------------- #
    def search(
        self,
        keyword: str,
        city: str,
        max_results: int = 20,
        on_progress=None,
    ) -> list[Place]:
        query = f"{keyword} في {city}" if city else keyword
        logger.info("Searching: %s", query)
        self._goto_search(query)
        links = self._scroll_results(max_results)
        # أبلغ العدد الحقيقي للروابط المجموعة
        if on_progress:
            try:
                on_progress(0, len(links))
            except Exception:
                pass
        results: list[Place] = []
        for i, link in enumerate(links, 1):
            logger.info("[%d/%d] %s", i, len(links), link[:70])
            try:
                if self._click_result(link):
                    place = self._extract_current_place(link)
                else:
                    # بديل: افتح الرابط مباشرة
                    place = self._extract_place_by_url(link)
                results.append(place)
                self._log_place(place, i, len(links))
            except Exception as e:
                logger.error("Failed: %s (%s)", link[:60], e)
                results.append(Place(place_url=link, timestamp=datetime.now().isoformat()))
            if on_progress:
                try:
                    on_progress(i, len(links))
                except Exception:
                    pass
            time.sleep(0.8)
        return results

    def _log_place(self, place: Place, idx: int, total: int) -> None:
        logger.info(
            "  → %s | ⭐%s (%s) | 📞%s | 🌐%s",
            place.name or "(no name)",
            place.rating or "-",
            place.reviews_count or "-",
            place.phone or "-",
            place.website or "-",
        )

    def extract_url(self, place_url: str) -> Place:
        """استخرج مكاناً واحداً من: رابط /maps/place/ مباشر، أو رابط بحث، أو اسم نصي.

        النص الذي ليس رابطاً يُعامَل كاستعلام بحث (يدعم وضع «القائمة/الملف» بالأسماء).
        """
        text = (place_url or "").strip()
        logger.info("Extracting single place: %s", text)
        if "/maps/place/" in text:
            return self._extract_place_by_url(text)

        page = self.page
        if text.lower().startswith(("http://", "https://")):
            page.goto(self._with_hl(text), wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            self._dismiss_consent()
        else:
            # اسم/كلمة بحث عادية → استخدم مسار البحث القياسي
            self._goto_search(text)

        self._scroll_results(1)
        a = page.locator('div[role="feed"] a[href*="/maps/place/"]').first
        if a.count() > 0:
            href = a.get_attribute("href") or ""
            if self._click_result(href):
                return self._extract_current_place(href)
            return self._extract_place_by_url(href)
        return Place(place_url=text, timestamp=datetime.now().isoformat())

    def extract_urls(self, urls, on_progress=None) -> list[Place]:
        urls = [u.strip() for u in urls if u and u.strip()]
        results: list[Place] = []
        if on_progress:
            try:
                on_progress(0, len(urls))
            except Exception:
                pass
        for i, u in enumerate(urls, 1):
            logger.info("[%d/%d] %s", i, len(urls), u)
            try:
                results.append(self.extract_url(u))
            except Exception as e:
                logger.error("Failed: %s (%s)", u, e)
                results.append(Place(place_url=u, timestamp=datetime.now().isoformat()))
            if on_progress:
                try:
                    on_progress(i, len(urls))
                except Exception:
                    pass
            time.sleep(0.8)
        return results


# --------------------------------------------------------------------------- #
# Output writers                                                              #
# --------------------------------------------------------------------------- #
def save_json(places: list[Place], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump([p.to_dict() for p in places], f, ensure_ascii=False, indent=2)
    logger.info("Saved %d records to %s", len(places), path)


def save_csv(places: list[Place], path: Path) -> None:
    if not places:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = list(places[0].to_dict().keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for p in places:
            row = p.to_dict()
            row["opening_hours"] = " | ".join(row["opening_hours"])
            writer.writerow(row)
    logger.info("Saved %d records to %s", len(places), path)
