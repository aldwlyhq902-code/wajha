"""فحص زر الساعات وتبويب الوصف بعد فتح لوحة تفاصيل مكان."""
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, locale="ar", viewport={"width":1366,"height":850})
    pg = ctx.new_page()
    pg.set_default_timeout(8000)

    pg.goto("https://www.google.com/maps/search/coffee in new york/",
            wait_until="domcontentloaded", timeout=40000)
    pg.wait_for_timeout(4000)

    # انقر أول نتيجة
    pg.locator('div[role="feed"] a[href*="/maps/place/"]').first.click()
    pg.wait_for_timeout(4000)
    print("PLACE:", pg.locator('h1.DUwDvf').first.inner_text())

    # ابحث عن كل العناصر التي تشير للساعات
    print("\n=== HOURS-related elements ===")
    for sel in [
        'button[aria-label*="ساعات" i]',
        'button[aria-label*="hours" i]',
        'button[aria-label*="open" i]',
        'button[aria-label*="مفتوح" i]',
        '[data-item-id^="oh"]',
        'span[aria-label*="ساعة" i]',
    ]:
        try:
            c = pg.locator(sel).count()
            if c > 0:
                el = pg.locator(sel).first
                txt = (el.inner_text(timeout=1000) or "").strip().replace("\n"," | ")[:80]
                aria = el.get_attribute("aria-label") or ""
                iid = el.get_attribute("data-item-id") or ""
                print(f"  [{sel}] count={c} text='{txt}' aria='{aria}' iid='{iid}'")
        except Exception as e:
            print(f"  [{sel}] err {e}")

    # النص الكامل للوحة التفاصيل
    print("\n=== detail panel full text (search 'مفتوح'/'يوم') ===")
    try:
        body = pg.inner_text('body', timeout=2000)
        for kw in ["مفتوح", "مغلق", "يوم", "السبت", "الأحد", "Open", "Closed", "Monday"]:
            if kw in body:
                idx = body.index(kw)
                snip = body[max(0,idx-30):idx+80].replace("\n"," | ")
                print(f"  '{kw}': ...{snip}...")
    except Exception:
        pass

    # ابحث عن تبويب "نظرة عامة" / About / نظرة
    print("\n=== TABS (Overview/About) ===")
    for sel in [
        'button[role="tab"]',
        'div[role="tab"]',
    ]:
        try:
            els = pg.locator(sel).all()
            for el in els:
                t = (el.inner_text(timeout=600) or "").strip()
                if t:
                    print(f"  tab: '{t}'")
        except Exception:
            pass

    # العناصر قبل/بعد ساعات
    print("\n=== elements with 'ساعة' or data-item-id oh ===")
    try:
        els = pg.locator('[data-item-id]').all()
        for el in els:
            iid = el.get_attribute("data-item-id") or ""
            if "oh" in iid.lower() or "hour" in iid.lower():
                txt = (el.inner_text(timeout=600) or "").strip().replace("\n"," | ")[:100]
                print(f"  iid='{iid}' text='{txt}'")
    except Exception:
        pass

    b.close()
