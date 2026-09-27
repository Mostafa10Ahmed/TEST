import asyncio
import os
import re
import base64
import json
from typing import List, Optional
from playwright.async_api import async_playwright, Page, Browser, BrowserContext
import asyncio as _asyncio

# playwright-stealth تغيّر أكثر من مرة: أحيانًا تكون stealth_async دالة،
# وأحيانًا يكون "stealth" موديل. نعمل طبقة توافق لتجنب خطأ:
# 'module' object is not callable
async def _apply_stealth(page: Page):
    try:
        # أحدث استخدام شائع
        from playwright_stealth import stealth_async  # type: ignore
        await stealth_async(page)  # type: ignore
        return
    except Exception:
        pass

    try:
        # بعض الإصدارات: stealth دالة
        from playwright_stealth import stealth  # type: ignore
        fn = stealth
        # أو stealth يكون موديل وفيه دالة stealth_async
        if not callable(fn) and hasattr(fn, "stealth_async"):
            fn = getattr(fn, "stealth_async")

        if callable(fn):
            res = fn(page)  # type: ignore
            if _asyncio.iscoroutine(res):
                await res
        return
    except Exception:
        # لو فشل stealth، نكمل بدونها بدل ما نكسر التحميل كله
        return
from bs4 import BeautifulSoup
import aiohttp

from .base_provider import BaseProvider, CHROME_HEADERS, get_cookies_for_url
class PlaywrightProvider(BaseProvider):
    def __init__(self):
        super().__init__()
        self.browser: Optional[Browser] = None
        self.playwright = None

    async def _ensure_browser(self):
        if not self.browser:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ]
            )

    async def close(self):
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def get_images(self, url: str) -> List[str]:
        """تحميل الصور باستخدام Playwright لتجاوز الحمايات المتطورة."""
        await self._ensure_browser()
        context = await self.browser.new_context(
            viewport={'width': 1280, 'height': 900},
            user_agent=CHROME_HEADERS['User-Agent']
        )
        
        # إضافة الكوكيز المسجلة للموقع (إذا وجدت)
        cookies_dict = get_cookies_for_url(url)
        if cookies_dict:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            playwright_cookies = []
            for name, value in cookies_dict.items():
                # تنظيف القيم من \n و \r وأحرف التحكم — Playwright يرفضها
                clean_name = str(name).strip().replace('\n', '').replace('\r', '')
                clean_value = str(value).strip().replace('\n', '').replace('\r', '')
                if not clean_name or clean_name.startswith('__custom_'):
                    continue
                playwright_cookies.append({
                    'name': clean_name,
                    'value': clean_value,
                    'domain': domain.replace('www.', '').lstrip('.'),
                    'path': '/'
                })
            if playwright_cookies:
                try:
                    await context.add_cookies(playwright_cookies)
                    print(f"[Playwright] Applied {len(playwright_cookies)} cookies for {domain}")
                except Exception as cookie_err:
                    print(f"[Playwright] Cookie error (skipping): {cookie_err}")

        # تجميع روابط الصور — نستخدم dict للحفاظ على الترتيب
        confirmed_images = []  # List يحافظ على ترتيب الإضافة
        
        async def handle_route(route, request):
            if request.resource_type == "image":
                confirmed_images.append(request.url)
            await route.continue_()

        await context.route("**/*", handle_route)
        
        page = await context.new_page()
        await _apply_stealth(page)
        
        try:
            print(f"[Playwright] Navigating to: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
            
            # التعامل مع الكابتشا إذا وجدت
            await self._handle_captcha(page)
            
            # التمرير السريع (Fast Scroll) لتفعيل Lazy Loading
            await self._fast_scroll(page)
            
            # البحث عن الصور في DOM (أكثر مصدر موثوق للترتيب)
            dom_images = await self._collect_from_dom(page)
            
            # البحث عن الصور في أكواد JavaScript (JS Source Scan)
            js_images = await self._collect_from_js(page)
            
            # دمج النتائج مع الحفاظ على الترتيب: DOM → JS → Network
            # نستخدم dict (Python ≥ 3.7 يحافظ على ترتيب الإضافة) لإزالة التكرارات
            seen: dict = {}
            for img in dom_images:
                seen[img] = len(seen)
            for img in js_images:
                if img not in seen:
                    seen[img] = len(seen)
            for img in confirmed_images:
                if img not in seen:
                    seen[img] = len(seen)
            
            # تصفية الروابط لضمان أنها صور حقيقية وتجنب الأيقونات الصغيرة والرموز الترويجية
            exclude_keywords = [
                "favicon", "logo", "avatar", "icon", "pixel", "tracking", "gravatar",
                "googleusercontent", "facebook", "twitter", "instagram", "discord",
                "banner", "header", "footer", "button", "advertisement", "promo",
                "widget", "comment", "user", "profile", "sprite", "spacer", "blank",
                "share", "telegram", "whatsapp", "cover", "thumb", "thumbnail", "poster",
                "wp-post-image", "lh3.google", "readerarea.svg"
            ]
            final_urls = []
            for img_url in seen:
                if not img_url.startswith("http"):
                    continue
                img_low = img_url.lower()
                if re.search(r'(?:^|[/._-])ads?(?:[/._-]|\d|$)', img_low):
                    continue
                if any(x in img_low for x in exclude_keywords):
                    continue
                if "qimanhwa" in url.lower() and "upload/series/" not in img_low:
                    continue
                final_urls.append(img_url)
                
            print(f"[Playwright] Found {len(final_urls)} images.")
            return final_urls

        except Exception as e:
            import traceback
            print(f"[Playwright] Error getting images: {type(e).__name__}")
            traceback.print_exc()
            return []
        finally:
            await context.close()

    async def _handle_captcha(self, page: Page):
        """كشف الكابتشا — انتظار قصير (بدون Gemini)."""
        body_text = await page.content()
        is_captcha = any(
            x in body_text.lower()
            for x in ("captcha", "robot", "verify you are human", "자동 입력 방지")
        )
        if not is_captcha:
            return
        print("[Playwright] CAPTCHA detected — waiting…")
        checkbox = await page.query_selector('iframe[src*="turnstile"]')
        if checkbox:
            try:
                await checkbox.click()
            except Exception:
                pass
        await asyncio.sleep(5)

    async def _fast_scroll(self, page: Page):
        """التمرير لأسفل الصفحة لتفعيل تحميل الصور الكسول."""
        await page.evaluate("""async () => {
            const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
            const scrollHeight = document.body.scrollHeight;
            const steps = 5;
            for (let i = 1; i <= steps; i++) {
                window.scrollTo(0, (scrollHeight / steps) * i);
                await delay(800);
            }
            window.scrollTo(0, document.body.scrollHeight);
            await delay(1500);
        }""")

    async def _collect_from_dom(self, page: Page) -> List[str]:
        return await page.evaluate("""() => {
            const selectors = [
                '.reading-content img', '#readerarea img', '.chapter-content img',
                '.viewer-container img', '.manga-reader img', '.entry-content img',
                'img.wp-manga-chapter-img', '[class*="chapter"] img', '[class*="reader"] img',
                'img[data-src]', 'img[data-lazy-src]', 'img[data-original]'
            ];
            let imgs = [];
            for (const sel of selectors) {
                const found = Array.from(document.querySelectorAll(sel));
                if (found.length > 5) { imgs = found; break; }
            }
            if (imgs.length === 0) imgs = Array.from(document.querySelectorAll('img'));
            
            return imgs.map(img => {
                const src = img.getAttribute('data-src') || img.getAttribute('data-lazy-src') 
                         || img.getAttribute('data-original') || img.getAttribute('data-cfsrc')
                         || img.getAttribute('data-url') || img.getAttribute('data-lazy-load')
                         || img.getAttribute('data-actual-src') || img.getAttribute('data-imagesource')
                         || img.getAttribute('data-echo') || img.src || '';
                return src;
            }).filter(s => s.startsWith('http'));
        }""")

    async def _collect_from_js(self, page: Page) -> List[str]:
        """البحث عن روابط الصور داخل أكواد JavaScript."""
        return await page.evaluate(r"""() => {
            const urls = new Set();
            const pattern = /["'`](https?:\/\/[^"'`\\s]+\.(?:jpe?g|png|webp|gif)[^"'`\\s]*?)["'`]/gi;
            
            document.querySelectorAll('script:not([src])').forEach(s => {
                let m;
                while ((m = pattern.exec(s.textContent || '')) !== null) urls.add(m[1]);
            });
            
            try {
                const data = JSON.stringify(window.__DATA__ || window.CHAPTER_INFO || window.images || '');
                let m;
                while ((m = pattern.exec(data)) !== null) urls.add(m[1]);
            } catch {}
            
            return Array.from(urls);
        }""")

    async def fetch_html_playwright(self, url: str) -> Optional[str]:
        """جلب محتوى الصفحة HTML باستخدام Playwright لتجاوز حماية Cloudflare."""
        await self._ensure_browser()
        context = await self.browser.new_context(
            viewport={'width': 1280, 'height': 900},
            user_agent=CHROME_HEADERS['User-Agent']
        )
        page = await context.new_page()
        await _apply_stealth(page)
        try:
            print(f"[Playwright] Fetching HTML for: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
            content = await page.content()
            return content
        except Exception as e:
            import traceback
            print(f"[Playwright] Error fetching HTML: {type(e).__name__}")
            traceback.print_exc()
            return None
        finally:
            await context.close()
