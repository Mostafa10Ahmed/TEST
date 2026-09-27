import asyncio
import json
import re
from typing import Optional

from .base_provider import CHROME_HEADERS, BaseProvider, get_cookies_for_url

try:
    from playwright.async_api import async_playwright  # type: ignore
    _PLAYWRIGHT_PROVIDER_AVAILABLE = True
except Exception:
    async_playwright = None  # type: ignore
    _PLAYWRIGHT_PROVIDER_AVAILABLE = False


class ComixProvider(BaseProvider):
    """
    مزود comix.to — نسخة محسّنة
    
    الاستراتيجية:
    1. جلب الفصول: JSON.parse monkey-patch في Playwright (يلتقط البيانات المفكوكة التشفير)
       مع دعم pagination كامل
    2. جلب الصور: نفس الأسلوب — JSON.parse يلتقط result.pages المفككة
    3. fallback: HTML parsing لـ #initial-data إذا كان متاحاً
    
    ملاحظات تقنية:
    - الـ API يعيد {"e": "blob مشفر"} — لا يمكن فك تشفيره في Python
    - الصور من /i3/ غير مشوشة (scrambling غير نشط حالياً)
    - الـ baseUrl في بيانات الصور قد يكون فارغاً عندما تكون روابط الصور كاملة
    """

    DOMAIN = "comix.to"

    def __init__(self):
        super().__init__()

    # ── URL helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _extract_hid(series_url: str) -> Optional[str]:
        """Extract short manga HID from URL: /title/{hid}-{slug} → hid"""
        m = re.search(r"/title/([^/?#]+)", series_url)
        if not m:
            return None
        slug = m.group(1)
        hid_m = re.match(r"^([A-Za-z0-9]+)-", slug)
        return hid_m.group(1) if hid_m else slug.split("-")[0]

    @staticmethod
    def _extract_chapter_id(chapter_url: str) -> Optional[str]:
        """Extract numeric chapter ID: /title/hid-slug/{chapter_id}-chapter-N"""
        m = re.search(r"/title/[^/]+/(\d+)-chapter-", chapter_url)
        return m.group(1) if m else None

    # ── Parse #initial-data JSON (SSR fallback) ──────────────────────────────
    @staticmethod
    def _parse_initial_data(html: str) -> Optional[dict]:
        """Extract and parse the #initial-data JSON embedded in the page."""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", id="initial-data")
        if not tag or not tag.string:
            return None
        try:
            return json.loads(tag.string)
        except Exception:
            return None

    # ── Browser context helpers ───────────────────────────────────────────────
    @staticmethod
    def _get_browser_args() -> list:
        return [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
        ]

    @staticmethod
    def _get_user_agent() -> str:
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    async def _make_browser_context(self, p):
        """Create a Playwright browser context with comix.to cookies and Cloudflare stealth overrides."""
        browser = await p.chromium.launch(
            headless=True,
            args=self._get_browser_args()
        )
        ctx = await browser.new_context(
            user_agent=self._get_user_agent(),
            viewport={"width": 1366, "height": 768},
            device_scale_factor=1,
            has_touch=False,
            is_mobile=False,
            java_script_enabled=True,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {} };
        """)

        # Inject saved cookies if we have any
        cookies = get_cookies_for_url("https://comix.to")
        if cookies:
            domain = ".comix.to"
            playwright_cookies = []
            for name, value in cookies.items():
                playwright_cookies.append({
                    "name": name,
                    "value": str(value),
                    "domain": domain,
                    "path": "/",
                })
            if playwright_cookies:
                await ctx.add_cookies(playwright_cookies)

        return browser, ctx

    # ── CHAPTERS monkey-patch init script ─────────────────────────────────────
    _CHAPTERS_INIT_SCRIPT = """
        window.__ALL_CHAPTERS__ = [];
        window.__CHAPTER_META__ = null;
        const __origParse_chap__ = JSON.parse;
        JSON.parse = function(text, reviver) {
            const result = __origParse_chap__(text, reviver);
            try {
                if (result && typeof result === 'object' &&
                    result.status === 'ok' && result.result &&
                    result.result.items && Array.isArray(result.result.items) &&
                    result.result.items.length > 0 &&
                    (result.result.items[0].mangaId !== undefined ||
                     result.result.items[0].number !== undefined)) {
                    window.__ALL_CHAPTERS__ = window.__ALL_CHAPTERS__.concat(result.result.items);
                    if (result.result.meta) {
                        window.__CHAPTER_META__ = result.result.meta;
                    }
                }
            } catch(e) {}
            return result;
        };
    """

    # ── IMAGES monkey-patch init script ──────────────────────────────────────
    _IMAGES_INIT_SCRIPT = """
        window.__CHAPTER_PAGES__ = null;
        const __origParse_img__ = JSON.parse;
        JSON.parse = function(text, reviver) {
            const result = __origParse_img__(text, reviver);
            try {
                if (result && typeof result === 'object' &&
                    result.status === 'ok' && result.result &&
                    result.result.pages && result.result.pages.items &&
                    Array.isArray(result.result.pages.items)) {
                    window.__CHAPTER_PAGES__ = result.result.pages;
                }
            } catch(e) {}
            return result;
        };
    """

    # ── get_all_chapters ─────────────────────────────────────────────────────
    async def get_all_chapters(self, series_url: str) -> dict:
        hid = self._extract_hid(series_url)
        if not hid:
            return {}

        # Layer 1: Try SSR #initial-data (fast, no Playwright)
        html = self.fetch_html(series_url)
        if html:
            chapters = self._chapters_from_initial_data(html, series_url)
            if chapters:
                print(f"[Comix] SSR: {len(chapters)} chapters from initial-data")
                return chapters

        # Layer 2: Playwright — JSON.parse monkey-patch with full pagination
        if not _PLAYWRIGHT_PROVIDER_AVAILABLE:
            from bot_config import Config
            if Config.HF_WORKER_URL:
                worker_url = Config.HF_WORKER_URL.rstrip("/")
                api_key = Config.HF_WORKER_KEY or Config.WEB_PANEL_SECRET
                headers = {}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                try:
                    print(f"[Comix] Playwright unavailable locally; delegating chapter extraction to HF Worker: {series_url}")
                    import requests as _requests
                    resp = _requests.post(
                        f"{worker_url}/extract/chapters",
                        json={"url": series_url},
                        headers=headers,
                        timeout=300
                    )
                    if resp.status_code == 200:
                        res = resp.json()
                        if res and res.get("ok") and res.get("chapters"):
                            chapters = {}
                            for k, v in res["chapters"].items():
                                try:
                                    chapters[float(k)] = v
                                except ValueError:
                                    pass
                            if chapters:
                                print(f"[Comix] HF Worker returned {len(chapters)} chapters")
                                return chapters
                except Exception as ex:
                    print(f"[Comix] HF Worker chapter delegation failed: {ex}")
            print(f"[Comix] Playwright unavailable; cannot fetch chapters for: {series_url}")
            return {}

        print(f"[Comix] Using Playwright to fetch all chapters: {series_url}")
        return await self._playwright_get_all_chapters(series_url, hid)

    def _chapters_from_initial_data(self, html: str, series_url: str) -> dict:
        """Extract chapter list from #initial-data SSR JSON."""
        data = self._parse_initial_data(html)
        if not data:
            return {}
        chapters: dict[float, str] = {}
        queries = data.get("queries", {})
        for key, value in queries.items():
            if not isinstance(value, dict):
                continue
            items = value.get("items", [])
            if not items:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                num = item.get("number")
                url = item.get("url", "")
                if num is not None and url and "chapter" in url.lower():
                    try:
                        n = float(num)
                        if 0 < n < 9999:
                            full_url = "https://comix.to" + url if url.startswith("/") else url
                            chapters[n] = full_url
                    except Exception:
                        pass
        return chapters

    def _chapters_from_html_links(self, html: str, series_url: str) -> dict:
        """
        Fallback: extract chapters from links embedded in HTML.
        FIXED: extracts the chapter NUMBER (-chapter-N) not the chapter ID.
        """
        hid = self._extract_hid(series_url) or ""
        if not hid:
            return {}
        chapters: dict[float, str] = {}
        try:
            # Pattern captures: /title/{hid}-.../{chapter_id}-chapter-{chapter_num}
            pattern = re.compile(
                rf"(/title/{re.escape(hid)}-[^/]+/\d+-chapter-(\d+(?:\.\d+)?))"
            )
            seen_nums: set[float] = set()
            for match in pattern.finditer(html or ""):
                try:
                    chapter_num = float(match.group(2))
                    if 0 < chapter_num < 9999 and chapter_num not in seen_nums:
                        seen_nums.add(chapter_num)
                        chapters[chapter_num] = "https://comix.to" + match.group(1)
                except Exception:
                    continue
        except Exception:
            pass
        return chapters

    def _items_to_chapters(self, items: list, existing: dict) -> dict:
        """Convert raw API/JS chapter items to {chapter_num: url} dict."""
        chapters = dict(existing)
        for item in (items or []):
            if not isinstance(item, dict):
                continue
            num = item.get("number")
            url = item.get("url", "")
            if num is None or not url:
                continue
            try:
                n = float(num)
                if 0 < n < 9999 and n not in chapters:
                    full_url = "https://comix.to" + url if url.startswith("/") else url
                    chapters[n] = full_url
            except Exception:
                pass
        return chapters

    async def _setup_page_and_navigate(self, context, url: str, init_script: str = None) -> tuple:
        from playwright.async_api import Page
        page = await context.new_page()
        
        # Apply stealth to prevent detection
        try:
            from .playwright_provider import _apply_stealth
            await _apply_stealth(page)
        except Exception:
            pass

        if init_script:
            await page.add_init_script(init_script)
            
        env_url_holder = []
        chapter_json_holder = []

        async def handle_request(request):
            req_url = request.url
            if req_url.endswith(".js") and ("/dist/env-" in req_url or "/env-" in req_url or "dist/main-" in req_url or "dist/ReadPage-" in req_url):
                env_url_holder.append(req_url)

        async def handle_response(response):
            res_url = response.url
            if "/chapters/" in res_url or "/api/v1/chapters" in res_url:
                try:
                    data = await response.json()
                    if data and isinstance(data, dict):
                        chapter_json_holder.append(data)
                except Exception:
                    pass

        page.on("request", handle_request)
        page.on("response", handle_response)
        
        try:
            print(f"[Comix][PW] Navigating to: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[Comix][PW] Navigation error: {e}")
            await page.wait_for_timeout(5000)
            
        # Handle CAPTCHA / Cloudflare Turnstile if present
        try:
            body_text = await page.content()
            if any(x in body_text.lower() for x in ("captcha", "robot", "verify you are human", "turnstile", "challenges.cloudflare")):
                print("[Comix][PW] CAPTCHA / Cloudflare challenge detected — waiting for Turnstile...")
                for _ in range(15):
                    await asyncio.sleep(1)
                    checkbox = await page.query_selector('iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"], iframe[title*="Cloudflare"]')
                    if checkbox:
                        try:
                            await checkbox.click()
                        except Exception:
                            pass
                        try:
                            box = await checkbox.bounding_box()
                            if box:
                                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        except Exception:
                            pass
                    body_text = await page.content()
                    if not any(x in body_text.lower() for x in ("verify you are human", "turnstile", "challenges.cloudflare")):
                        print("[Comix][PW] CAPTCHA solved/redirected! Waiting for React render...")
                        try:
                            await page.wait_for_selector("script#initial-data, div#app", timeout=15000)
                        except Exception:
                            await asyncio.sleep(5)
                        break
        except Exception as e:
            print(f"[Comix][PW] CAPTCHA handling error: {e}")
            
        return page, env_url_holder, chapter_json_holder

    async def _playwright_get_all_chapters(self, series_url: str, hid: str) -> dict:
        """
        JSON parse intercepting + Native Pagination clicking.
        """
        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            print(f"[Comix] Playwright unavailable: {e}")
            return {}

        chapters: dict[float, str] = {}

        async with async_playwright() as p:
            browser, context = await self._make_browser_context(p)

            try:
                page, _, _ = await self._setup_page_and_navigate(context, "about:blank")
                
                await page.add_init_script(self._CHAPTERS_INIT_SCRIPT)
                
                try:
                    await page.goto(series_url, wait_until="networkidle", timeout=60000)
                except Exception:
                    await page.wait_for_timeout(5000)

                await page.wait_for_timeout(2000)

                items = await page.evaluate("() => window.__ALL_CHAPTERS__")
                meta = await page.evaluate("() => window.__CHAPTER_META__")
                
                last_page = 1
                if isinstance(meta, dict):
                    last_page = meta.get("lastPage", 1) or 1
                
                # Fetch more pages via native UI clicking if available
                if last_page > 1:
                    print(f"[Comix][PW] Found {last_page} pages, clicking through pagination...")
                    for i in range(2, last_page + 1):
                        clicked = await page.evaluate(f"""(pageNum) => {{
                            const el = document.evaluate(`//button[normalize-space(text())='${{pageNum}}'] | //a[normalize-space(text())='${{pageNum}}'] | //div[normalize-space(text())='${{pageNum}}']`, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                            if (el) {{ el.click(); return true; }}
                            return false;
                        }}""", i)
                        if clicked:
                            await page.wait_for_timeout(1500)
                
                # Re-fetch items after pagination clicking
                items = await page.evaluate("() => window.__ALL_CHAPTERS__")
                chapters = self._items_to_chapters(items, chapters)
                
            finally:
                await context.close()
                await browser.close()

        print(f"[Comix][PW] Total: {len(chapters)} chapters")
        return chapters

    # ── get_images ───────────────────────────────────────────────────────────
    async def get_images(self, chapter_url: str) -> list:
        cid = self._extract_chapter_id(chapter_url)

        # Layer 1: Parse #initial-data from chapter page HTML (fast, SSR)
        html = self.fetch_html(chapter_url, {"Referer": "https://comix.to/"})
        if html:
            images = self._images_from_initial_data(html)
            if images:
                print(f"[Comix] SSR: {len(images)} images from initial-data")
                return images

        # Layer 2: Playwright — JSON.parse monkey-patch (confirmed working)
        if not _PLAYWRIGHT_PROVIDER_AVAILABLE:
            from bot_config import Config
            if Config.HF_WORKER_URL:
                worker_url = Config.HF_WORKER_URL.rstrip("/")
                api_key = Config.HF_WORKER_KEY or Config.WEB_PANEL_SECRET
                headers = {}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                try:
                    print(f"[Comix] Playwright unavailable locally; delegating image extraction to HF Worker: {chapter_url}")
                    import requests as _requests
                    resp = _requests.post(
                        f"{worker_url}/extract/images",
                        json={"url": chapter_url},
                        headers=headers,
                        timeout=300
                    )
                    if resp.status_code == 200:
                        res = resp.json()
                        if res and res.get("ok") and res.get("images"):
                            print(f"[Comix] HF Worker returned {len(res['images'])} images")
                            return res["images"]
                except Exception as ex:
                    print(f"[Comix] HF Worker image delegation failed: {ex}")
            print(f"[Comix] Playwright unavailable; cannot fetch images")
            return []

        print(f"[Comix] Using Playwright to fetch images: {chapter_url}")
        return await self._playwright_get_images(chapter_url)

    async def _descramble_items_in_browser(self, page, raw_items: list) -> list:
        if not raw_items:
            return []
        print(f"[Comix][PW] Unscrambling {len(raw_items)} pages via browser context descrambler (secure.t)...")
        try:
            descrambled_res = await page.evaluate("""async (items) => {
                try {
                    const readPageUrl = Array.from(document.querySelectorAll('script')).map(s => s.src).find(s => s.includes('ReadPage-')) || 'https://comix.to/assets/build/35595e3de3c99889c1aa70/dist/ReadPage-tj9sr6-IxVqlQSv.js';
                    const readPageText = await (await fetch(readPageUrl)).text();
                    const secureMatch = readPageText.match(/import\s*\{[^}]*t\s*as\s*ms[^}]*\}\s*from\s*["']([^"']+)["']/);
                    const secureUrl = new URL(secureMatch[1], readPageUrl).href;
                    const secure = await import(secureUrl);

                    let finalUrls = [];
                    for (const item of items) {
                        const imgUrl = typeof item === 'string' ? item : item.url;
                        const isScrambled = typeof item === 'object' && item.scramble;
                        if (!isScrambled) {
                            finalUrls.push(imgUrl);
                            continue;
                        }

                        const descrambler = await secure.t(imgUrl);
                        const canvas = document.createElement('canvas');
                        canvas.width = item.width || 800;
                        canvas.height = item.height || 1200;
                        descrambler.apply(canvas);
                        await new Promise(r => setTimeout(r, 1200));

                        finalUrls.push(canvas.toDataURL('image/jpeg', 0.95));
                    }
                    return { ok: true, urls: finalUrls };
                } catch (e) {
                    return { ok: false, error: String(e) };
                }
            }""", raw_items)

            if descrambled_res.get("ok") and descrambled_res.get("urls"):
                return descrambled_res["urls"]
        except Exception as e:
            print(f"[Comix][PW] Browser descrambler error: {e}")

        return [item["url"] if isinstance(item, dict) else item for item in raw_items]

    def _images_from_initial_data(self, html: str) -> list:
        """Extract chapter page images from #initial-data SSR JSON."""
        data = self._parse_initial_data(html)
        if not data:
            return []
        queries = data.get("queries", {})
        for key, value in queries.items():
            if not isinstance(value, dict):
                continue
            pages_data = value.get("pages", {})
            if isinstance(pages_data, dict) and pages_data.get("items"):
                return self._parse_pages_data(pages_data)
        return []

    def _parse_pages_data(self, pages_data: dict) -> list:
        """
        Convert pages API object to list of absolute image URLs or dicts with metadata.
        """
        if not pages_data:
            return []
        base = pages_data.get("baseUrl", "") or ""
        items = pages_data.get("items", []) or []
        images = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url", "")
            if not url:
                continue
            if not url.startswith("http") and base:
                url = base.rstrip("/") + "/" + url.lstrip("/")
            if url.startswith("http"):
                images.append({
                    "url": url,
                    "scramble": item.get("s") == 1 or item.get("scramble") == 1,
                    "width": item.get("width", 800),
                    "height": item.get("height", 1200)
                })
        return images

    async def _playwright_get_images(self, chapter_url: str) -> list:
        """
        Dynamic Env API image fetching.
        Intercepts the env bundle URL, imports it, and uses env.b client to fetch chapter metadata (containing pages).
        Falls back to monkey-patch if dynamic import fails.
        """
        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            print(f"[Comix] Playwright unavailable: {e}")
            return []

        images = []
        cid = self._extract_chapter_id(chapter_url)
        if not cid:
            return []

        async with async_playwright() as p:
            browser, context = await self._make_browser_context(p)

            try:
                page, env_url_holder, chapter_json_holder = await self._setup_page_and_navigate(context, chapter_url, init_script=self._IMAGES_INIT_SCRIPT)

                # Wait up to 10s for API response or window.__CHAPTER_PAGES__
                for _ in range(100):
                    if chapter_json_holder:
                        break
                    pages_check = await page.evaluate("() => window.__CHAPTER_PAGES__")
                    if pages_check:
                        break
                    await asyncio.sleep(0.1)

                # Layer A: Intercepted API response
                if chapter_json_holder:
                    print(f"[Comix][PW] Captured chapter API response directly!")
                    for item in chapter_json_holder:
                        pages = item.get("pages", {}) if isinstance(item, dict) else {}
                        if isinstance(pages, dict) and pages.get("items"):
                            raw_items = self._parse_pages_data(pages)
                            if raw_items:
                                descrambled = await self._descramble_items_in_browser(page, raw_items)
                                if descrambled:
                                    return descrambled

                # Layer B: Check #initial-data in rendered DOM
                html_content = await page.content()
                if html_content:
                    ssr_images = self._images_from_initial_data(html_content)
                    if ssr_images:
                        print(f"[Comix][PW] Captured {len(ssr_images)} images from #initial-data DOM!")
                        descrambled = await self._descramble_items_in_browser(page, ssr_images)
                        if descrambled:
                            return descrambled

                # Layer B: Monkey-patched window.__CHAPTER_PAGES__
                pages_data = await page.evaluate("() => window.__CHAPTER_PAGES__")
                if pages_data and isinstance(pages_data, dict):
                    print(f"[Comix][PW] Captured __CHAPTER_PAGES__ via init_script!")
                    raw_items = self._parse_pages_data(pages_data)
                    if raw_items:
                        descrambled = await self._descramble_items_in_browser(page, raw_items)
                        if descrambled:
                            return descrambled

                # Fallback: scrape img elements from rendered page
                img_srcs = await page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('img'))
                        .map(i => i.src)
                        .filter(s => s && s.includes('wowpic') && s.startsWith('http'));
                }""")
                if img_srcs:
                    images = img_srcs

            finally:
                await context.close()
                await browser.close()

        print(f"[Comix][PW] Total: {len(images)} images")
        return images

    # ── get_latest_chapter ───────────────────────────────────────────────────
    def get_latest_chapter(self, url: str) -> Optional[float]:
        """Get the latest chapter number from the series page."""
        try:
            html = self.fetch_html(url)
            if html:
                data = self._parse_initial_data(html)
                if data:
                    manga = data.get("manga", {})
                    latest = manga.get("latestChapter")
                    if latest and isinstance(latest, (int, float)):
                        return float(latest)
                    # Also check queries
                    for _k, v in data.get("queries", {}).items():
                        if isinstance(v, dict) and v.get("latestChapter"):
                            return float(v["latestChapter"])
                # Fallback: extract chapter numbers from HTML links
                links = self._chapters_from_html_links(html, url)
                if links:
                    return max(links.keys())
        except Exception:
            pass
        return None
