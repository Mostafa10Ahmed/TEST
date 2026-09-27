import re
import json
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
from urllib.parse import urljoin, urlparse


class KakaoProvider(BaseProvider):
    """
    مزود Kakao Page / Kakao Webtoon — الفصول المجانية والمجدولة.
    يدعم: page.kakao.com, webtoon.kakao.com
    """

    def __init__(self):
        super().__init__()
        self.headers.update({
            "Referer":  "https://page.kakao.com/",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        })

    def _is_webtoon(self, url: str) -> bool:
        return "webtoon.kakao.com" in url

    def _extract_series_id(self, url: str) -> str:
        m = re.search(r'/content/(\d+)', url)
        if not m:
            m = re.search(r'/webtoon/(\d+)', url)
        if not m:
            m = re.search(r'seriesId=(\d+)', url)
        return m.group(1) if m else None

    def _extract_chapter_id(self, url: str) -> str:
        m = re.search(r'/episode/(\d+)', url)
        if not m:
            m = re.search(r'/viewer/(\d+)', url)
        if not m:
            m = re.search(r'episodeId=(\d+)', url)
        return m.group(1) if m else None

    async def get_images(self, url: str):
        try:
            series_id = self._extract_series_id(url)
            ch_id = self._extract_chapter_id(url)
            
            # 1. Try BFF viewer data API first
            if series_id and ch_id:
                api_url = f"https://bff-page.kakao.com/api/gateway/api/v1/viewer/data?series_id={series_id}&product_id={ch_id}"
                headers = self.headers.copy()
                headers["Referer"] = f"https://page.kakao.com/content/{series_id}/viewer/{ch_id}"
                async with aiohttp.ClientSession(headers=headers) as session:
                    try:
                        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                            if r.status == 200:
                                data = await r.json()
                                viewer_data = data.get("viewerData") or {}
                                img_download_data = viewer_data.get("imageDownloadData") or {}
                                files = img_download_data.get("files") or []
                                if files:
                                    images = []
                                    for f in files:
                                        if isinstance(f, dict) and "secureUrl" in f:
                                            images.append(f["secureUrl"])
                                        elif isinstance(f, str):
                                            images.append(f)
                                    if images:
                                        return images
                    except Exception as e:
                        print(f"[Kakao] BFF viewer/data API error: {e}")

            html = self.fetch_html(url)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")

            # طريقة 1: __NEXT_DATA__
            nd = soup.find("script", id="__NEXT_DATA__")
            if nd:
                try:
                    data  = json.loads(nd.string)
                    text  = json.dumps(data)
                    imgs  = re.findall(
                        r'"(https?://[^"]+(?:kakaocdn|kakao)[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
                        text
                    )
                    clean = []
                    for img in imgs:
                        src = img.replace("\\u002F", "/").replace("\\", "")
                        if src not in clean:
                            clean.append(src)
                    if clean:
                        return clean
                except Exception:
                    pass

            # طريقة 2: API داخلي
            ch_id = self._extract_chapter_id(url)
            if ch_id:
                api_urls = [
                    f"https://page.kakao.com/api/viewerData?episodeId={ch_id}",
                    f"https://webtoon.kakao.com/api/viewerData?episodeId={ch_id}",
                ]
                async with aiohttp.ClientSession(headers=self.headers) as session:
                    for api_url in api_urls:
                        try:
                            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                                if r.status == 200:
                                    data = await r.json()
                                    imgs = re.findall(
                                        r'"(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
                                        json.dumps(data)
                                    )
                                    if imgs:
                                        return [i.replace("\\u002F", "/") for i in imgs]
                        except Exception:
                            continue

            # طريقة 3: سكريبتات وصور مباشرة
            images = []
            for img in soup.select("img[src*='kakaocdn'], img[src*='kakao']"):
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("http") and src not in images:
                    images.append(src)
            return images

        except Exception as e:
            print(f"[Kakao] get_images error: {e}")
            return []

    async def get_chapters_with_lock_info(self, series_url: str, bypass_fallback: bool = False) -> dict:
        try:
            series_id = self._extract_series_id(series_url)
            if not series_id:
                return {}

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://page.kakao.com/",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
            }

            chapters = {}
            cursor_index = 0
            cursor_direction = "ANCHOR"
            
            async with aiohttp.ClientSession(headers=headers) as session:
                while True:
                    api_url = (
                        f"https://bff-page.kakao.com/api/gateway/api/v2/content/product/list"
                        f"?series_id={series_id}&cursor_index={cursor_index}"
                        f"&cursor_direction={cursor_direction}&window_size=100"
                    )
                    try:
                        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                            if r.status == 200:
                                data = await r.json()
                                result = data.get("result", {})
                                lst = result.get("list", [])
                                has_next = result.get("has_next", False)
                                
                                if not lst:
                                    break
                                    
                                for raw_item in lst:
                                    item = raw_item.get("item", {})
                                    pid = item.get("product_id") or item.get("uid")
                                    order_value = item.get("order_value")
                                    is_free = item.get("is_free", False)
                                    
                                    if pid and order_value is not None:
                                        try:
                                            num = float(order_value)
                                            ch_url = f"https://page.kakao.com/content/{series_id}/viewer/{pid}"
                                            chapters[num] = {
                                                "url": ch_url,
                                                "locked": not is_free,
                                                "reason": "kakao-bff-api",
                                            }
                                        except Exception:
                                            pass
                                
                                if not has_next:
                                    break
                                    
                                cursor_index = lst[-1].get("cursor_index", 0)
                                cursor_direction = "NEXT"
                            else:
                                print(f"[KakaoLock] BFF API failed with status {r.status}")
                                break
                    except Exception as e:
                        print(f"[KakaoLock] BFF API error: {e}")
                        break
            
            if not chapters and not bypass_fallback:
                print(f"[KakaoLock] BFF failed, falling back to HTML parsing via get_all_chapters")
                all_chs = await self.get_all_chapters(series_url)
                for num, ch_url in all_chs.items():
                    chapters[num] = {
                        "url": ch_url,
                        "locked": False,
                        "reason": "kakao-html-fallback",
                    }
            return chapters
        except Exception as e:
            print(f"[Kakao] get_chapters_with_lock_info error: {e}")
            return {}

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            # Try BFF API first
            lock_info = await self.get_chapters_with_lock_info(series_url, bypass_fallback=True)
            if lock_info:
                return {num: item["url"] for num, item in lock_info.items()}
        except Exception as e:
            print(f"[Kakao] get_all_chapters custom error: {e}")

        # Legacy fallback
        try:
            series_id = self._extract_series_id(series_url)
            if not series_id:
                return {}

            chapters = {}
            page     = 1

            async with aiohttp.ClientSession(headers=self.headers) as session:
                while True:
                    for api_base in [
                        "https://page.kakao.com/api",
                        "https://webtoon.kakao.com/api",
                    ]:
                        try:
                            ep_url = f"{api_base}/episodeList?seriesId={series_id}&page={page}&size=100"
                            async with session.get(ep_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                                if r.status == 200:
                                    data = await r.json()
                                    eps  = data.get("data", {}).get("episodeList", []) or data.get("episodes", [])
                                    if eps:
                                        for ep in eps:
                                            ep_id  = ep.get("id") or ep.get("episodeId")
                                            ep_num = ep.get("episodeSequence") or ep.get("order") or ep.get("number")
                                            if ep_id and ep_num is not None:
                                                try:
                                                    num = float(ep_num)
                                                    ch_url = f"https://page.kakao.com/content/{series_id}/viewer/{ep_id}"
                                                    if num not in chapters:
                                                        chapters[num] = ch_url
                                                except Exception:
                                                    pass
                                        if len(eps) < 100:
                                            break
                                        page += 1
                                        continue
                        except Exception:
                            continue
                    break

            # Fallback: HTML scraping
            if not chapters:
                html = self.fetch_html(series_url)
                if (not html or "__NEXT_DATA__" not in html) and hasattr(self, "playwright") and self.playwright:
                    print(f"[Kakao] Fallback to Playwright HTML fetch for: {series_url}")
                    html = await self.playwright.fetch_html_playwright(series_url)
                if html:
                    soup = BeautifulSoup(html, "html.parser")
                    nd = soup.find("script", id="__NEXT_DATA__")
                    if nd:
                        try:
                            text = json.dumps(json.loads(nd.string))
                            for m in re.finditer(r'"episode(?:Id|Sequence)"\s*:\s*(\d+)', text):
                                pass
                            ep_pairs = re.findall(
                                r'"id"\s*:\s*(\d+).*?"episodeSequence"\s*:\s*(\d+)',
                                text
                            )
                            for ep_id, ep_seq in ep_pairs:
                                try:
                                    num = float(ep_seq)
                                    chapters[num] = f"https://page.kakao.com/content/{series_id}/viewer/{ep_id}"
                                except Exception:
                                    pass
                        except Exception:
                            pass

            return chapters
        except Exception as e:
            print(f"[Kakao] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(result.keys()) if result else None
