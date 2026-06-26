"""فحص النمط الصحيح: بحث -> النقر على نتيجة -> فحص لوحة التفاصيل."""
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=UA, locale="ar", viewport={"width":1366,"height":850})
    pg = ctx.new_page()

    # 1) بحث
    pg.goto("https://www.google.com/maps/search/coffee in new york/",
            wait_until="domcontentloaded", timeout=40000)
    pg.wait_for_timeout(4000)
    print("TITLE:", pg.title())

    # 2) أنقر أول نتيجة في القائمة
    feed = pg.locator('div[role="feed"]')
    print("feed count:", feed.count())

    # عناصر النتائج عادة role="article" أو a مع aria-label
    articles = pg.locator('div[role="feed"] a[href*="/maps/place/"]')
    print("result links:", articles.count())

    if articles.count() > 0:
        # انقر على أول نتيجة
        articles.first.click(timeout=10000)
        pg.wait_for_timeout(5000)
        print("\n=== AFTER CLICK ===")
        print("H1:", repr(pg.locator('h1.DUwDvf').first.inner_text(timeout=3000)))

        # افحص كل العناصر التي تحوي معلومات (data-item-id شائع بعد الفتح)
        print("\n=== data-item-id elements (THE KEY SELECTORS) ===")
        els = pg.locator('[data-item-id]').all()
        print("count:", len(els))
        for el in els:
            try:
                iid = el.get_attribute("data-item-id")
                tag = el.evaluate("e => e.tagName")
                txt = (el.inner_text(timeout=600) or "").strip().replace("\n"," ")[:70]
                href = el.get_attribute("href") or ""
                print(f"  [{tag}] id='{iid}' text='{txt}' href='{href[:50]}'")
            except Exception:
                pass

        # التصنيف والتقييم
        print("\n=== category ===")
        for sel in ['button[jsaction*="pane.rating.category"]', 'button[jsaction*="category"]']:
            try:
                if pg.locator(sel).count() > 0:
                    print(f"  {sel}: '{pg.locator(sel).first.inner_text(timeout=2000)}'")
            except Exception as e:
                print(f"  {sel}: {e}")

        print("\n=== rating block ===")
        for sel in ['div.F7nice', 'div[aria-label*="نجم"]', 'span[aria-hidden="true"]']:
            try:
                c = pg.locator(sel).count()
                if c > 0:
                    txt = pg.locator(sel).first.inner_text(timeout=1500)
                    print(f"  {sel} ({c}): '{txt[:60]}'")
            except Exception:
                pass

        pg.screenshot(path="debug_after_click.png")
        print("\nscreenshot saved: debug_after_click.png")

    b.close()
