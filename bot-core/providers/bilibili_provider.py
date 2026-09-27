import re
import json
import asyncio
import aiohttp
from .base_provider import BaseProvider
from urllib.parse import urlparse


class BilibiliProvider(BaseProvider):
    """
    مزود Bilibili Manga (manga.bilibili.com) — الفصول المجانية فقط.
    يستخدم API رسمي.
    """

    API = "https://manga.bilibili.com/twirp/comic.v1.Comic"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://manga.bilibili.com/",
        "Origin":  "https://manga.bilibili.com",
    }

    def __init__(self):
        super().__init__()

    def _extract_comic_id(self, url: str) -> str:
        m = re.search(r'/mc(\d+)', url)
        return m.group(1) if m else None

    def _extract_ep_id(self, url: str) -> str:
        m = re.search(r'/mc\d+/(\d+)', url)
        return m.group(1) if m else None

    async def get_images(self, url: str):
        try:
            ep_id = self._extract_ep_id(url)
            if not ep_id:
                return []

            async with aiohttp.ClientSession(headers=self.HEADERS) as session:
                # جلب فهرس الصور
                payload = {"ep_id": int(ep_id)}
                async with session.post(
                    f"{self.API}/GetImageIndex",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status != 200:
                        return []
                    data = await r.json()

                if data.get("code") != 0:
                    print(f"[Bilibili] GetImageIndex error: {data.get('msg')}")
                    return []

                images_raw = data.get("data", {}).get("images", [])
                if not images_raw:
                    return []

                # جلب URLs الحقيقية
                paths = [img["path"] for img in images_raw]
                async with session.post(
                    f"{self.API}/ImageToken",
                    json={"urls": json.dumps(paths)},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r2:
                    if r2.status != 200:
                        return []
                    token_data = await r2.json()

                images = []
                for item in token_data.get("data", []):
                    img_url = item.get("url", "")
                    token   = item.get("token", "")
                    if img_url:
                        full = f"{img_url}?token={token}" if token else img_url
                        images.append(full)
                return images
        except Exception as e:
            print(f"[Bilibili] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            comic_id = self._extract_comic_id(series_url)
            if not comic_id:
                return {}

            async with aiohttp.ClientSession(headers=self.HEADERS) as session:
                async with session.post(
                    f"{self.API}/ComicDetail",
                    json={"comic_id": int(comic_id)},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status != 200:
                        return {}
                    data = await r.json()

            if data.get("code") != 0:
                return {}

            chapters = {}
            ep_list  = data.get("data", {}).get("ep_list", [])
            for ep in ep_list:
                is_locked = ep.get("is_locked", True)
                ep_id     = ep.get("id")
                ord_val   = ep.get("ord")
                title     = ep.get("title", "")
                if not ep_id or not ord_val:
                    continue
                try:
                    num = float(ord_val)
                    ch_url = f"https://manga.bilibili.com/mc{comic_id}/{ep_id}"
                    if not is_locked and num not in chapters:
                        chapters[num] = ch_url
                    elif is_locked:
                        # نضع الفصول المدفوعة بعلامة
                        pass
                except Exception:
                    pass

            # لو ما فيه مجاني، نضع كلها لمعرفة البنية
            if not chapters:
                for ep in ep_list[:50]:
                    ep_id   = ep.get("id")
                    ord_val = ep.get("ord")
                    if ep_id and ord_val:
                        try:
                            num = float(ord_val)
                            chapters[num] = f"https://manga.bilibili.com/mc{comic_id}/{ep_id}"
                        except Exception:
                            pass

            return chapters
        except Exception as e:
            print(f"[Bilibili] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(result.keys()) if result else None
