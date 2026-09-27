import re
import asyncio
import aiohttp
from .base_provider import BaseProvider


class MangaPlusProvider(BaseProvider):
    """
    مزود MangaPlus (Shueisha) — رسمي ومجاني.
    يستخدم API غير رسمي مدروس.
    يدعم: mangaplus.shueisha.co.jp
    """

    API = "https://jumpg-webapi.tokyo-cdn.com/api"
    HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://mangaplus.shueisha.co.jp/",
        "Accept": "application/octet-stream",
    }

    def __init__(self):
        super().__init__()

    def _extract_title_id(self, url: str) -> str:
        m = re.search(r'/titles/(\d+)', url)
        return m.group(1) if m else None

    def _extract_chapter_id(self, url: str) -> str:
        m = re.search(r'/viewer/(\d+)', url)
        return m.group(1) if m else None

    async def get_images(self, url: str):
        try:
            chapter_id = self._extract_chapter_id(url)
            if not chapter_id:
                return []
            api_url = f"{self.API}/manga_viewer?chapter_id={chapter_id}&split=yes&img_quality=super_high"
            async with aiohttp.ClientSession(headers=self.HEADERS) as session:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        return []
                    raw = await r.read()
            images = re.findall(
                rb'https://[a-zA-Z0-9\-_.]+/[^\x00-\x1f\x7f"\'<> ]+?\.(?:webp|jpg|jpeg|png)',
                raw
            )
            return [img.decode("utf-8") for img in images]
        except Exception as e:
            print(f"[MangaPlus] get_images error: {e}")
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            title_id = self._extract_title_id(series_url)
            if not title_id:
                return {}
            api_url = f"{self.API}/title_detailV3?title_id={title_id}"
            async with aiohttp.ClientSession(headers=self.HEADERS) as session:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        return {}
                    raw = await r.read()

            chapters = {}
            chapter_ids = re.findall(rb'\x08(\xd0[\x80-\xff]+|\xc0[\x80-\xff]+|\xe0[\x80-\xff]+|[\x80-\xff]{1,4})\x10', raw)
            ch_pattern = re.findall(
                rb'chapter_id:(\d+).*?chapter_number:([\d.]+)',
                raw.replace(b'\x00', b'')
            )

            ids_raw = re.findall(rb'"chapter_id":(\d+),"chapter_number":"([\d.]+)"', raw)
            for ch_id, ch_num in ids_raw:
                try:
                    num = float(ch_num.decode())
                    cid = ch_id.decode()
                    url_ch = f"https://mangaplus.shueisha.co.jp/viewer/{cid}"
                    if num not in chapters:
                        chapters[num] = url_ch
                except Exception:
                    pass

            if not chapters:
                nums = re.findall(rb'#(\d+)', raw)
                viewer_ids = re.findall(rb'/viewer/(\d+)', raw)
                for i, vid in enumerate(viewer_ids):
                    try:
                        num = float(i + 1)
                        chapters[num] = f"https://mangaplus.shueisha.co.jp/viewer/{vid.decode()}"
                    except Exception:
                        pass

            return chapters
        except Exception as e:
            print(f"[MangaPlus] get_all_chapters error: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(result.keys()) if result else None
