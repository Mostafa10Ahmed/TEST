import re
import json
import asyncio
import aiohttp
from typing import Optional
from bs4 import BeautifulSoup
from .base_provider import BaseProvider, fetch_with_curl


class ComickProvider(BaseProvider):
    """
    مزود Comick (comick.io / comick.fun / comick.cc)
    يدعم استخراج البيانات المباشرة من __NEXT_DATA__ وواجهة REST API
    """

    API = "https://api.comick.io"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://comick.io/",
        })

    def _extract_slug(self, url: str) -> str:
        m = re.search(r'/comic/([^/?#]+)', url)
        return m.group(1) if m else None

    def _extract_chapter_hid(self, url: str) -> str:
        m = re.search(r'/chapter/([^/?#]+)', url)
        if m:
            return m.group(1)
        # Check /comic/slug/{hid}-chapter-...
        m = re.search(r'/comic/[^/]+/([a-zA-Z0-9_-]+)-chapter-', url)
        return m.group(1) if m else None

    async def get_images(self, url: str) -> list[str]:
        try:
            # 1. Try HTML scraping with NEXT_DATA
            html = fetch_with_curl(url) if fetch_with_curl else self.fetch_html(url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                nd = soup.find("script", id="__NEXT_DATA__")
                if nd and nd.string:
                    try:
                        data = json.loads(nd.string)
                        ch = data.get("props", {}).get("pageProps", {}).get("chapter", {})
                        md_images = ch.get("md_images", []) or ch.get("images", [])
                        res = []
                        for img in md_images:
                            b2key = img.get("b2key", "") if isinstance(img, dict) else ""
                            if b2key:
                                res.append(f"https://meo.comick.pictures/{b2key}")
                            elif isinstance(img, dict) and img.get("url"):
                                res.append(img["url"])
                            elif isinstance(img, str) and img.startswith("http"):
                                res.append(img)
                        if res:
                            return res
                    except Exception:
                        pass

            # 2. REST API fallback
            hid = self._extract_chapter_hid(url)
            if hid:
                async with aiohttp.ClientSession(headers=self.headers) as session:
                    async with session.get(
                        f"{self.API}/chapter/{hid}",
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            chapter = data.get("chapter", {})
                            md_images = chapter.get("md_images", [])
                            res = []
                            for img in md_images:
                                b2key = img.get("b2key", "")
                                if b2key:
                                    res.append(f"https://meo.comick.pictures/{b2key}")
                            if res:
                                return res
        except Exception as e:
            print(f"[Comick] get_images error: {e}")
        return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            slug = self._extract_slug(series_url)
            if not slug:
                return {}

            chapters = {}

            # 1. Extract from NEXT_DATA via curl_cffi / fetch_html
            clean_url = f"https://comick.io/comic/{slug}"
            html = fetch_with_curl(clean_url) if fetch_with_curl else self.fetch_html(clean_url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                nd = soup.find("script", id="__NEXT_DATA__")
                if nd and nd.string:
                    try:
                        data = json.loads(nd.string)
                        pp = data.get("props", {}).get("pageProps", {})
                        first_chs = pp.get("firstChapters", [])
                        for ch in first_chs:
                            ch_num = ch.get("chap")
                            ch_hid = ch.get("hid")
                            ch_lang = ch.get("lang", "en")
                            if ch_num and ch_hid:
                                try:
                                    num = float(ch_num)
                                    url_ch = f"https://comick.io/comic/{slug}/{ch_hid}-chapter-{ch_num}-{ch_lang}"
                                    if num not in chapters:
                                        chapters[num] = url_ch
                                except Exception:
                                    pass
                        if chapters:
                            return chapters
                    except Exception:
                        pass

            # 2. REST API fallback
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(
                    f"{self.API}/comic/{slug}",
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status == 200:
                        comic_data = await r.json(content_type=None)
                        hid = comic_data.get("comic", {}).get("hid")
                        if hid:
                            page = 1
                            while True:
                                params = {
                                    "lang": "en,ar",
                                    "page": page,
                                    "limit": 300,
                                    "order": "asc",
                                }
                                async with session.get(
                                    f"{self.API}/comic/{hid}/chapters",
                                    params=params,
                                    timeout=aiohttp.ClientTimeout(total=15)
                                ) as r_ch:
                                    if r_ch.status != 200:
                                        break
                                    data = await r_ch.json(content_type=None)
                                    chs = data.get("chapters", [])
                                    if not chs:
                                        break
                                    for ch in chs:
                                        ch_num = ch.get("chap")
                                        ch_hid = ch.get("hid")
                                        if ch_num and ch_hid:
                                            try:
                                                num = float(ch_num)
                                                url_ch = f"https://comick.io/comic/{slug}/{ch_hid}-chapter-{ch_num}-en"
                                                if num not in chapters:
                                                    chapters[num] = url_ch
                                            except Exception:
                                                pass
                                    if len(chs) < 300:
                                        break
                                    page += 1
            return chapters
        except Exception as e:
            print(f"[Comick] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(result.keys()) if result else None

    async def quick_check_latest(self, slug_or_url: str) -> Optional[float]:
        try:
            slug = self._extract_slug(slug_or_url) if "http" in slug_or_url else slug_or_url
            if not slug:
                return None

            clean_url = f"https://comick.io/comic/{slug}"
            html = fetch_with_curl(clean_url) if fetch_with_curl else self.fetch_html(clean_url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                nd = soup.find("script", id="__NEXT_DATA__")
                if nd and nd.string:
                    data = json.loads(nd.string)
                    last_ch = data.get("props", {}).get("pageProps", {}).get("comic", {}).get("last_chapter")
                    if last_ch is not None:
                        return float(last_ch)
        except Exception as e:
            print(f"[Comick] quick_check_latest error: {e}")
        return None
