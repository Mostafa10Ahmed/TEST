import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base_provider import BaseProvider, fetch_with_curl, get_cookies_for_url


class AsuraProvider(BaseProvider):
    """مزود AsuraScans - يستخدم Next.js مع بيانات JSON مضمّنة"""

    DOMAINS = ["asurascans.com", "asura.gg", "asuracomics.com", "asuratoon.com", "asuracomic.net", "asura.nacm.xyz", "asurascan.com"]

    def __init__(self):
        super().__init__()
        self.base_url = "https://asurascans.com"
        self.headers["Referer"] = self.base_url + "/"

    def _normalize_url(self, url: str) -> str:
        for domain in self.DOMAINS:
            if domain in url:
                return url
        return url

    def _unwrap_rsc(self, value):
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
            return self._unwrap_rsc(value[1])
        if isinstance(value, list):
            return [self._unwrap_rsc(item) for item in value]
        if isinstance(value, dict):
            return {k: self._unwrap_rsc(v) for k, v in value.items()}
        return value

    def _extract_rsc_data(self, html: str) -> dict | None:
        import html as ht

        matches = re.finditer(r'props="(\{&quot;.*?\})"', html, re.DOTALL)
        for m in matches:
            try:
                content = ht.unescape(m.group(1))
                raw_data = json.loads(content)
                data = self._unwrap_rsc(raw_data)
                if isinstance(data, dict) and (
                    "chapters" in data
                    or "chapterList" in data
                    or "pages" in data
                    or "totalChapters" in data
                ):
                    return data
            except Exception:
                pass
        return None

    def _filter_chapter_images(self, images: list[str]) -> list[str]:
        if not images:
            return []
        clean = []
        for img in images:
            low = img.lower()
            if any(
                x in low
                for x in [
                    "logo",
                    "avatar",
                    "icon",
                    "banner",
                    "cover",
                    "thumb",
                    "poster",
                    "preload",
                    "facebook",
                    "discord",
                    "gif",
                    "gifs",
                    "favorite",
                    "favorites",
                    "comment",
                    "comments",
                    "badge",
                    "badges",
                    "sticker",
                    "stickers",
                    "emoji",
                    "emojis",
                    "reaction",
                ]
            ):
                continue
            clean.append(img)

        # Strict priority filter: Asura chapter page images ALWAYS contain /chapters/ in path
        # E.g. https://cdn.asurascans.com/asura-images/chapters/goblin-inc/1/f6a3f2.webp
        chapter_only = [
            img for img in clean if "/chapters/" in img.lower()
        ]
        if chapter_only:
            return chapter_only
        return clean

    def _extract_page_urls(self, payload) -> list[str]:
        """Extract page image URLs from all known Asura API/RSC shapes."""
        images: list[str] = []
        seen: set[str] = set()

        def add(src):
            if not src:
                return
            src = str(src).replace("\\u002F", "/").replace("\\/", "/").strip()
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http") and src not in seen:
                if not any(
                    x in src.lower()
                    for x in ["logo", "avatar", "icon", "banner", "cover", "thumb", "poster", "preload"]
                ):
                    seen.add(src)
                    images.append(src)

        def walk(value):
            if isinstance(value, dict):
                for key in (
                    "url",
                    "src",
                    "image",
                    "image_url",
                    "imageUrl",
                    "page_url",
                    "pageUrl",
                    "path",
                ):
                    if key in value:
                        add(value.get(key))
                for key in ("pages", "chapter_pages", "chapterPages", "images"):
                    if key in value:
                        walk(value.get(key))
                # Do not recursively walk every dict key or cover URLs will dominate.
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, str):
                add(value)

        if isinstance(payload, dict):
            data = payload.get("data", payload)
            walk(data.get("pages") if isinstance(data, dict) else data)
            if isinstance(data, dict):
                chapter = data.get("chapter") or {}
                walk(chapter.get("pages"))
                walk(chapter.get("chapter_pages"))
                walk(chapter.get("images"))
        else:
            walk(payload)
        return self._filter_chapter_images(images)

    def _fetch_chapter_api(self, url: str, cookies: dict) -> dict | None:
        m = re.search(r"/comics/([^/]+)/chapter/([^/?#]+)", url)
        if not m or not cookies.get("access_token"):
            return None
        series_slug = m.group(1)
        chapter_slug = m.group(2)
        api_url = f"https://api.asurascans.com/api/series/{series_slug}/chapters/{chapter_slug}"
        headers = {
            "Authorization": f"Bearer {cookies['access_token']}",
            "Accept": "application/json",
            "Origin": "https://asurascans.com",
            "Referer": url,
        }
        if "__custom_user_agent" in cookies:
            headers["User-Agent"] = cookies["__custom_user_agent"]
        
        # Fast Cloudflare bypass via curl_cffi
        raw = fetch_with_curl(api_url, headers=headers, timeout=12)
        if raw:
            try:
                import json as _json
                return _json.loads(raw)
            except Exception:
                pass
        return self.fetch_json(api_url, extra_headers=headers)

    async def get_images(self, url: str) -> list[str]:
        try:
            images = []

            # Try the same API Asura's frontend prefetches from localStorage access_token.
            from .base_provider import get_cookies_for_url

            cookies = get_cookies_for_url(url)
            if "access_token" in cookies:
                try:
                    resp = self._fetch_chapter_api(url, cookies)
                    images = self._extract_page_urls(resp)
                    if images:
                        return self._filter_chapter_images(images)
                    if resp and isinstance(resp, dict):
                        data = resp.get("data", {})
                        if data.get("is_locked") or data.get("unlock_time"):
                            print(
                                f"[AsuraScans API] chapter locked or pages unavailable: {data.get('unlock_time') or 'locked'}"
                            )
                except Exception as e:
                    print(f"[AsuraScans API error] {e}")

            html = self.fetch_html(url)
            if not html:
                return []

            # fallback to HTML parsing
            data = self._extract_rsc_data(html)
            if data:
                images = self._extract_page_urls(data)
                if images:
                    return self._filter_chapter_images(images)

            soup = BeautifulSoup(html, "html.parser")
            def add_image(src: str | None) -> None:
                if not src:
                    return
                src = (
                    str(src)
                    .replace("\\u002F", "/")
                    .replace("\\/", "/")
                    .strip()
                )
                if src.startswith("//"):
                    src = "https:" + src
                if not src.startswith("http") or src in images:
                    return
                if any(
                    x in src.lower()
                    for x in ["logo", "avatar", "icon", "banner", "cover", "thumb", "poster", "preload"]
                ):
                    return
                images.append(src)

            # Asura now renders chapter pages as plain <img> tags without a
            # dedicated reader container, so scan the whole document first.
            for img in soup.find_all("img"):
                src = (
                    img.get("data-src")
                    or img.get("data-lazy-src")
                    or img.get("data-original")
                    or img.get("src")
                )
                add_image(src)
                if not src and img.get("srcset"):
                    best_src = img.get("srcset").split(",")[-1].strip().split(" ")[0]
                    add_image(best_src)

            reader_divs = [
                soup.find("div", id="readerarea"),
                soup.select_one(".reading-content"),
                soup.select_one('[class*="reader"]'),
                soup.select_one('[id*="reader"]'),
                soup.select_one(".chapter-content"),
            ]
            for div in reader_divs:
                if not div:
                    continue
                for img in div.find_all("img"):
                    add_image(
                        img.get("data-src")
                        or img.get("data-lazy-src")
                        or img.get("data-original")
                        or img.get("src")
                    )

            if not images:
                images = self._extract_images_from_json(html)

            return self._filter_chapter_images(images)
        except Exception as e:
            print(f"[AsuraScans] get_images error: {e}")
            return []

    def _extract_images_from_json(self, text: str) -> list:
        images = []
        patterns = [
            r'"src"\s*:\s*"(https?://[^"]+\.(?:webp|jpg|jpeg|png)[^"]*)"',
            r'"url"\s*:\s*"(https?://[^"]+\.(?:webp|jpg|jpeg|png)[^"]*)"',
            r'https?://[a-zA-Z0-9\-_.]+/[^"\'\s<>]+?\.(?:webp|jpg|jpeg|png)',
        ]
        for pattern in patterns:
            for match in re.findall(pattern, text, re.IGNORECASE):
                if isinstance(match, tuple):
                    match = match[0]
                cleaned = (
                    match.replace("\\u002F", "/")
                    .replace("\\n", "")
                    .replace("\\", "")
                    .strip()
                    .rstrip('"')
                )
                if cleaned.startswith("http") and cleaned not in images:
                    if not any(
                        x in cleaned.lower()
                        for x in ["logo", "avatar", "icon", "banner", "cover", "thumb", "poster", "preload"]
                    ):
                        images.append(cleaned)
        return self._filter_chapter_images(images)

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            series_url = re.sub(r"/chapter[s]?/.*", "", series_url, flags=re.IGNORECASE).rstrip("/")
            html = self.fetch_html(series_url)
            if not html:
                return {}

            # 1. محاولة RSC props
            data = self._extract_rsc_data(html)
            if data and "chapters" in data:
                chapters = {}
                clean_series_url = series_url.rstrip("/")
                for ch in data["chapters"]:
                    num_val = ch.get("number")
                    name = ch.get("name")
                    if num_val is not None:
                        try:
                            num = float(num_val)
                            name_str = str(int(num)) if num.is_integer() else str(num)
                            if name:
                                name_str = str(name).strip()
                            chapters[num] = f"{clean_series_url}/chapter/{name_str}"
                        except Exception:
                            pass
                if chapters:
                    return chapters

            soup = BeautifulSoup(html, "html.parser")
            chapters = {}

            # 2. محاولة __NEXT_DATA__
            next_data = soup.find("script", id="__NEXT_DATA__")
            if next_data:
                try:
                    data = json.loads(next_data.string)
                    text = json.dumps(data)
                    parsed = urlparse(series_url)
                    base = f"{parsed.scheme}://{parsed.netloc}"
                    for m in re.finditer(
                        r'"((?:https?://[^"]+|/[^"]+)/chapter[s]?/(\d+(?:\.\d+)?))"',
                        text,
                    ):
                        href = m.group(1).replace("\\u002F", "/").replace("\\", "")
                        num = float(m.group(2))
                        if not href.startswith("http"):
                            href = urljoin(base, href)
                        if "/chapter" in href.lower():
                            chapters[num] = href

                    if not chapters:
                        for m in re.finditer(
                            r'"slug"\s*:\s*"([^"]+)"\s*.*?"chapterNumber"\s*:\s*(\d+(?:\.\d+)?)',
                            text,
                        ):
                            slug = m.group(1)
                            num = float(m.group(2))
                            if "/chapter/" not in slug:
                                chapters[num] = f"{series_url.rstrip('/')}/{slug}"
                            else:
                                chapters[num] = urljoin(base, slug)

                    if chapters:
                        return chapters
                except Exception:
                    pass

            # 3. من الروابط التقليدية
            parsed = urlparse(series_url)
            base = f"{parsed.scheme}://{parsed.netloc}"

            def _extract(h, b):
                s = BeautifulSoup(h, "html.parser")
                res = {}
                selectors = [
                    "div.eph-num a",
                    "li.wp-manga-chapter a",
                    'a[href*="/chapter"]',
                    'a[href*="chapter-"]',
                ]
                for sel in selectors:
                    for a in s.select(sel):
                        href = a.get("href", "").strip()
                        if not href:
                            continue
                        if not href.startswith("http"):
                            href = urljoin(base, href)
                        m = re.search(r"chapter[s]?[-/](\d+(?:\.\d+)?)", href, re.I)
                        if not m:
                            m = re.search(
                                r"chapter[s]?[-/](\d+(?:\.\d+)?)", a.get_text(), re.I
                            )
                        if m:
                            num = float(m.group(1))
                            if num not in res:
                                res[num] = href
                return res

            chapters = _extract(html, series_url)
            extra = self._paginate_chapters(series_url, _extract)
            chapters.update(extra)

            return chapters
        except Exception as e:
            print(f"[AsuraScans] get_all_chapters error: {e}")
            return {}

    async def get_chapters_with_lock_info(self, series_url: str) -> dict:
        try:
            series_url = re.sub(r"/chapter[s]?/.*", "", series_url, flags=re.IGNORECASE).rstrip("/")
            html = self.fetch_html(series_url)
            if not html:
                return {}

            data = self._extract_rsc_data(html)
            if data and "chapters" in data:
                chapters = {}
                clean_series_url = series_url.rstrip("/")
                for ch in data["chapters"]:
                    num_val = ch.get("number")
                    name = ch.get("name")
                    if num_val is not None:
                        try:
                            num = float(num_val)
                            name_str = str(int(num)) if num.is_integer() else str(num)
                            if name:
                                name_str = str(name).strip()

                            locked = bool(ch.get("is_locked", False))
                            reason = "rsc-locked" if locked else "rsc-free"
                            unlock_time = ch.get("unlock_time")

                            chapters[num] = {
                                "url": f"{clean_series_url}/chapter/{name_str}",
                                "locked": locked,
                                "reason": reason,
                                **({"unlock_time": unlock_time} if unlock_time else {}),
                            }
                        except Exception:
                            pass
                if chapters:
                    return chapters

            # Fallback to get_all_chapters if RSC parsing didn't find chapters
            all_ch = await self.get_all_chapters(series_url)
            if all_ch:
                return {
                    num: {"url": ch_url, "locked": False, "reason": "asura-fallback-all"}
                    for num, ch_url in all_ch.items()
                }
            return {}
        except Exception as e:
            print(f"[AsuraProvider] get_chapters_with_lock_info error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        import asyncio

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(result.keys()) if result else None
