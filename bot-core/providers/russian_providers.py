"""
مزودات المواقع الروسية (Russian Manga Providers):
- MangaLib & HentaiLib (api.lib.social / mangalib.me / hentailib.me)
- MangaBuff (mangabuff.ru)
- Grouple (ReadManga, SeiManga, Usagi, MintManga)
"""

import re
import json
import asyncio
import aiohttp
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

from .base_provider import BaseProvider


# ─────────────────────────────────────────────────────────────────
#  MangaLib / HentaiLib Provider (Lib.social API)
# ─────────────────────────────────────────────────────────────────
class MangaLibProvider(BaseProvider):
    """
    مزود شبكة Lib الروسية (MangaLib / HentaiLib / SlashLib / YaoiLib)
    يعتمد على واجهة REST API المباشرة عبر api.lib.social
    """

    DOMAINS = [
        "mangalib.me",
        "mangalib.org",
        "v2.mangalib.org",
        "hentailib.me",
        "slashlib.me",
        "yaoilib.me",
        "lib.social",
    ]

    API_BASE = "https://api.lib.social/api"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
            "Site-Id": "1",
        })

    def _extract_slug(self, url: str) -> str:
        m = re.search(r'/manga/([^/?#]+)', url)
        if m:
            return m.group(1)
        m = re.search(r'/ru/manga/([^/?#]+)', url)
        if m:
            return m.group(1)
        parsed = urlparse(url)
        parts = [p for p in parsed.path.strip("/").split("/") if p and p not in ("ru", "en")]
        return parts[-1] if parts else None

    def _get_site_id(self, url: str) -> str:
        if "hentai" in url:
            return "2"
        if "slash" in url or "yaoi" in url:
            return "3"
        return "1"

    async def get_all_chapters(self, series_url: str) -> dict:
        slug = self._extract_slug(series_url)
        if not slug:
            return {}

        site_id = self._get_site_id(series_url)
        headers = dict(self.headers)
        headers["Site-Id"] = site_id

        chapters = {}
        api_url = f"{self.API_BASE}/manga/{slug}/chapters"
        try:
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        ch_list = data.get("data", [])
                        for ch in ch_list:
                            vol = ch.get("volume", 1)
                            num_str = str(ch.get("number", ""))
                            ch_id = ch.get("id")
                            try:
                                num = float(num_str) if num_str else float(ch_id)
                            except ValueError:
                                num = float(ch_id)
                            
                            ch_url = f"https://mangalib.me/ru/manga/{slug}/read/v{vol}/c{num_str}"
                            chapters[num] = ch_url
                        if chapters:
                            return chapters
        except Exception as e:
            print(f"[MangaLib] API chapters fetch failed: {e}")

        # Fallback: Parse HTML
        try:
            html = self.fetch_html(series_url)
            if html:
                m_state = re.search(r'window\.__DATA__\s*=\s*({.+?});', html, re.S)
                if m_state:
                    data = json.loads(m_state.group(1))
                    for ch in data.get("chapters", {}).get("list", []):
                        num = float(ch.get("number", 0))
                        vol = ch.get("volume", 1)
                        chapters[num] = f"https://mangalib.me/ru/manga/{slug}/read/v{vol}/c{num}"
        except Exception as e:
            print(f"[MangaLib] HTML fallback failed: {e}")

        return chapters

    async def get_images(self, url: str) -> list[str]:
        slug = self._extract_slug(url)
        m_vol = re.search(r'/v(\d+)', url)
        m_ch = re.search(r'/c([\d.]+)', url)

        vol = m_vol.group(1) if m_vol else "1"
        ch = m_ch.group(1) if m_ch else "1"

        site_id = self._get_site_id(url)
        headers = dict(self.headers)
        headers["Site-Id"] = site_id
        cdn_host = "https://img3h.hentaicdn.org" if site_id == "2" else "https://img3.cdnlibs.org"

        if slug:
            endpoint = f"{self.API_BASE}/manga/{slug}/chapter"
            params = {"number": ch, "volume": vol}
            try:
                async with aiohttp.ClientSession(headers=headers) as s:
                    async with s.get(endpoint, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            pages = data.get("data", {}).get("pages", [])
                            res = []
                            for p in pages:
                                p_url = p.get("url") or p.get("image")
                                if p_url:
                                    if not p_url.startswith("http"):
                                        p_url = f"{cdn_host}{p_url}"
                                    res.append(p_url)
                            if res:
                                return res
            except Exception as e:
                print(f"[MangaLib] get_images API failed: {e}")

        html = self.fetch_html(url)
        if html:
            images = re.findall(r'https?://[^\s"\'<>]*(?:cdnlibs\.org|hentaicdn\.org)[^\s"\'<>]*', html)
            return list(dict.fromkeys(images))

        return []


# ─────────────────────────────────────────────────────────────────
#  MangaBuff Provider (mangabuff.ru)
# ─────────────────────────────────────────────────────────────────
class MangaBuffProvider(BaseProvider):
    """
    مزود MangaBuff (mangabuff.ru) — موقع روسي مفتوح وسريع.
    """

    DOMAINS = ["mangabuff.ru"]
    BASE = "https://mangabuff.ru"

    def __init__(self):
        super().__init__()
        self.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
            "Referer": "https://mangabuff.ru/",
        })

    def _extract_slug(self, url: str) -> str:
        m = re.search(r'/manga/([^/?#]+)', url)
        return m.group(1) if m else None

    async def get_all_chapters(self, series_url: str) -> dict:
        slug = self._extract_slug(series_url)
        if not slug:
            return {}

        clean_url = f"{self.BASE}/manga/{slug}"
        html = self.fetch_html(clean_url)
        if not html:
            return {}

        soup = BeautifulSoup(html, "html.parser")
        ch_links = soup.select(f"a[href*='/manga/{slug}/']")
        if not ch_links:
            ch_links = soup.select("a[href*='/manga/']")

        chapters = {}
        for a in ch_links:
            href = a.get("href", "")
            if not href.startswith("http"):
                href = urljoin(self.BASE, href)
            m = re.search(rf'/manga/{re.escape(slug)}/(\d+)/([\d.]+)', href)
            if m:
                vol, ch_str = m.group(1), m.group(2)
                try:
                    num = float(ch_str)
                    if num not in chapters:
                        chapters[num] = href
                except ValueError:
                    pass
            else:
                txt = a.get_text(strip=True)
                nm = self.extract_chapter_number(txt)
                if nm is not None and nm not in chapters:
                    chapters[nm] = href

        return chapters

    async def get_images(self, url: str) -> list[str]:
        html = self.fetch_html(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        images = []
        for img in soup.select("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if src and ("chapters/" in src or "c3.mangabuff.ru" in src or "storage" in src):
                if not src.startswith("http"):
                    src = urljoin(self.BASE, src)
                if src not in images:
                    images.append(src)

        if not images:
            raw_imgs = re.findall(r'https?://[^\s"\'<>]+\.mangabuff\.ru/chapters/[^\s"\'<>]+', html)
            images = list(dict.fromkeys(raw_imgs))

        return images


# ─────────────────────────────────────────────────────────────────
#  Grouple Provider (ReadManga / SeiManga / Usagi / MintManga)
# ─────────────────────────────────────────────────────────────────
class GroupleProvider(BaseProvider):
    """
    مزود شبكة Grouple الروسية (ReadManga, SeiManga, Usagi, MintManga)
    مع دعم تجاوز شاشات المحتوى عبر ?mtr=true
    """

    DOMAINS = [
        "readmanga.live",
        "readmanga.me",
        "readmanga.app",
        "1.seimanga.me",
        "seimanga.me",
        "web.usagi.one",
        "usagi.one",
        "a.zazaza.me",
        "zazaza.me",
        "mintmanga.live",
        "mintmanga.me",
    ]

    def __init__(self):
        super().__init__()
        self.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        })

    def _ensure_mtr(self, url: str) -> str:
        if "mtr=true" not in url:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}mtr=true"
        return url

    async def get_all_chapters(self, series_url: str) -> dict:
        url = self._ensure_mtr(series_url)
        html = self.fetch_html(url)
        if not html:
            return {}

        soup = BeautifulSoup(html, "html.parser")
        chapters = {}

        for a in soup.select("a.chapter-link, .chapters-list a, tr.item a[href*='/vol']"):
            href = a.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                parsed = urlparse(series_url)
                base = f"{parsed.scheme}://{parsed.netloc}"
                href = urljoin(base, href)
            
            href = self._ensure_mtr(href)
            txt = a.get_text(strip=True)
            nm = self.extract_chapter_number(txt)
            if nm is not None and nm not in chapters:
                chapters[nm] = href

        return chapters

    async def get_images(self, url: str) -> list[str]:
        req_url = self._ensure_mtr(url)
        html = self.fetch_html(req_url)
        if not html:
            return []

        m_init = re.search(r'rm_h\.init(?:Reader)?\(\s*(\[.+?\])\s*,\s*0\s*,', html, re.S)
        if m_init:
            try:
                data_raw = m_init.group(1).replace("'", '"')
                parsed_list = json.loads(data_raw)
                images = []
                for item in parsed_list:
                    if isinstance(item, list) and len(item) >= 2:
                        full_img = f"{item[0]}{item[2]}" if len(item) > 2 and item[2].startswith("/") else f"{item[0]}{item[1]}"
                        images.append(full_img)
                    elif isinstance(item, str) and item.startswith("http"):
                        images.append(item)
                if images:
                    return images
            except Exception as e:
                print(f"[Grouple] JS reader parse error: {e}")

        images = re.findall(r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)', html)
        filtered = [i for i in images if "avatar" not in i and "banner" not in i and "logo" not in i]
        return list(dict.fromkeys(filtered))
