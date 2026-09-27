import re
import json
import aiohttp
import asyncio
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
from urllib.parse import urljoin, urlparse


class MangaFireProvider(BaseProvider):
    """
    مزود MangaFire.to — يستخدم scraping + API داخلي.
    """

    BASE = "https://mangafire.to"

    def __init__(self):
        super().__init__()
        self.headers["Referer"] = self.BASE + "/"

    def _extract_id(self, url: str) -> str:
        m = re.search(r'mangafire\.to/(?:manga|read)/([^/?#]+)', url)
        return m.group(1) if m else None

    def _extract_chapter_id(self, url: str) -> str:
        m = re.search(r'/read/[^/]+/([^/?#]+)', url)
        return m.group(1) if m else None

    async def get_images(self, url: str):
        try:
            chapter_id = self._extract_chapter_id(url)
            if not chapter_id:
                return []
            api_url = f"{self.BASE}/ajax/read/chapter/{chapter_id}"
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()
            images = []
            html_content = data.get("result", {}).get("html", "")
            if html_content:
                soup = BeautifulSoup(html_content, "html.parser")
                for img in soup.find_all("img"):
                    src = img.get("src") or img.get("data-src") or ""
                    if src.startswith("http") and src not in images:
                        images.append(src)
            return images
        except Exception as e:
            print(f"[MangaFire] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            manga_id = self._extract_id(series_url)
            if not manga_id:
                return {}
            manga_slug = manga_id.split(".")[-1] if "." in manga_id else manga_id
            api_url = f"{self.BASE}/ajax/manga/{manga_slug}/chapter/en"
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        return {}
                    data = await r.json()
            chapters = {}
            html_content = data.get("result", {}).get("html", "")
            if html_content:
                soup = BeautifulSoup(html_content, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if not href.startswith("http"):
                        href = urljoin(self.BASE, href)
                    m = re.search(r'chapter[/-](\d+(?:\.\d+)?)', href, re.I)
                    if m:
                        try:
                            chapters[float(m.group(1))] = href
                        except Exception:
                            pass
            return chapters
        except Exception as e:
            print(f"[MangaFire] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(result.keys()) if result else None
