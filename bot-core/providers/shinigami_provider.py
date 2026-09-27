"""
shinigami_provider.py — مزود g.shinigami.asia

الموقع محمي بـ Cloudflare Bot Management.
يستخدم curl_cffi مع browser impersonation لتجاوز الحماية.
نمط الروابط: /series/{uuid} و /chapter/{uuid}
"""

from __future__ import annotations
import re
import json
import asyncio
import os
import requests
from typing import Optional
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from .base_provider import BaseProvider

SHINIGAMI_HOME = "https://g.shinigami.asia"
SHINIGAMI_API  = "https://api.shngm.io/v1"
UUID_PATTERN   = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
IMG_EXT_RE     = re.compile(r"\.(?:webp|jpg|jpeg|png)(?:[?#].*)?$", re.I)


class ShinigamiProvider(BaseProvider):
    """
    مزود g.shinigami.asia
    يعتمد على curl_cffi لتجاوز Cloudflare ثم يحلل __NEXT_DATA__ أو HTML
    """

    DOMAINS = ["g.shinigami.asia", "shinigami.asia"]

    def __init__(self):
        super().__init__()
        self._cf_session = None

    # ── إدارة الجلسة ────────────────────────────────────────────────────────
    def _get_cf_session(self):
        if self._cf_session is not None:
            return self._cf_session
        try:
            from curl_cffi import requests as cfreq
            self._cf_session = cfreq.Session(impersonate="chrome131")
            self._cf_session.get(SHINIGAMI_HOME, timeout=15)
            print("[Shinigami] CF session initialized (chrome131)")
        except ImportError:
            print("[Shinigami] curl_cffi غير متاح — سيتم استخدام cloudscraper")
            self._cf_session = None
        except Exception as e:
            print(f"[Shinigami] session init error: {e}")
            self._cf_session = None
        return self._cf_session

    def _fetch(self, url: str, accept_json: bool = False) -> Optional[str]:
        session = self._get_cf_session()
        cookie = os.getenv("SHINIGAMI_COOKIE", "").strip()
        if not cookie:
            cf_clearance = os.getenv("SHINIGAMI_CF_CLEARANCE", "").strip()
            if cf_clearance:
                cookie = f"cf_clearance={cf_clearance}"
        headers = {
            "Referer": SHINIGAMI_HOME + "/",
            "Accept": ("application/json, text/javascript, */*; q=0.01"
                       if accept_json else
                       "text/html,application/xhtml+xml,*/*;q=0.9"),
            "Accept-Language": "en-US,en;q=0.9",
        }
        if cookie:
            headers["Cookie"] = cookie
        if session is not None:
            for imp in ["chrome131", "chrome124", "chrome120", "safari18_0"]:
                try:
                    session.impersonate = imp
                    r = session.get(url, headers=headers, timeout=25)
                    if r.status_code == 200 and len(r.text) > 200:
                        return r.text
                except Exception:
                    continue
        return self.fetch_html(url)

    # ── استخراج UUID ────────────────────────────────────────────────────────
    @staticmethod
    def _extract_uuid(url: str, segment: str) -> Optional[str]:
        m = re.search(rf"/{re.escape(segment)}/({UUID_PATTERN})", url)
        return m.group(1) if m else None

    # ── تحليل __NEXT_DATA__ ─────────────────────────────────────────────────
    def _walk_json(self, obj):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from self._walk_json(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from self._walk_json(v)
        else:
            yield obj

    @staticmethod
    def _normalize_url(url: str) -> str:
        if not isinstance(url, str):
            return ""
        cleaned = url.replace("\\/", "/").replace("\\u002F", "/").replace("\\", "").strip()
        if cleaned.startswith("//"):
            return "https:" + cleaned
        if cleaned.startswith("/"):
            return urljoin(SHINIGAMI_HOME, cleaned)
        return cleaned

    @staticmethod
    def _to_float(value) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            n = float(value)
            return n if 0 < n < 9999 else None
        if isinstance(value, str):
            m = re.search(r"\d+(?:\.\d+)?", value)
            if not m:
                return None
            n = float(m.group(0))
            return n if 0 < n < 9999 else None
        return None

    @classmethod
    def _extract_chapter_num(cls, item: dict) -> Optional[float]:
        for key in ("number", "chapterNumber", "chapter_number", "chapter", "order", "index", "title", "name"):
            n = cls._to_float(item.get(key))
            if n is not None:
                return n
        return None

    @classmethod
    def _extract_item_uuid(cls, item: dict) -> Optional[str]:
        for key in ("id", "chapterId", "_id", "slug", "uuid"):
            raw = item.get(key)
            if not isinstance(raw, str):
                continue
            m = re.search(UUID_PATTERN, raw, re.I)
            if m:
                return m.group(0)
        for key in ("href", "url", "path", "link"):
            raw = item.get(key)
            if not isinstance(raw, str):
                continue
            m = re.search(rf"/chapter/({UUID_PATTERN})", raw, re.I)
            if m:
                return m.group(1)
        return None

    def _chapters_from_next_data(self, html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        tag  = soup.find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            return {}

        try:
            raw  = json.loads(tag.string)
            text = json.dumps(raw)
        except Exception:
            return {}

        chapters: dict[float, str] = {}

        # استخراج هيكلي من JSON بدل regex هش.
        for item in self._walk_json(raw):
            if not isinstance(item, dict):
                continue
            uid = self._extract_item_uuid(item)
            num = self._extract_chapter_num(item)
            if uid and num is not None and num not in chapters:
                chapters[num] = f"{SHINIGAMI_HOME}/chapter/{uid}"

        if chapters:
            print(f"[Shinigami] NEXT_DATA structured: {len(chapters)} chapters")
            return chapters

        # Fallback سريع عبر regex لو البنية غير متوقعة.
        for m in re.finditer(
            rf'"((?:https?://[^"]+|/[^"]+)/chapter/({UUID_PATTERN}))"',
            text, re.I
        ):
            href = self._normalize_url(m.group(1))
            uid = m.group(2)
            start = max(0, m.start() - 350)
            snip = text[start: m.end() + 350]
            for key in ("number", "chapterNumber", "chapter_number", "chapter", "order", "index"):
                nm = re.search(rf'"{key}"\s*:\s*(\d+(?:\.\d+)?)', snip)
                if nm:
                    num = float(nm.group(1))
                    if 0 < num < 9999 and num not in chapters:
                        chapters[num] = href or f"{SHINIGAMI_HOME}/chapter/{uid}"
                    break
        return chapters

    # ── تحليل HTML العادي ───────────────────────────────────────────────────
    def _chapters_from_html(self, html: str, series_url: str) -> dict:
        soup     = BeautifulSoup(html, "html.parser")
        chapters : dict[float, str] = {}
        base     = f"{urlparse(series_url).scheme}://{urlparse(series_url).netloc}"

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("http"):
                href = urljoin(base, href)

            uid_m = re.search(rf"/chapter/({UUID_PATTERN})", href)
            if not uid_m:
                continue
            uid = uid_m.group(1)

            # رقم الفصل من النص
            text = a.get_text(strip=True)
            if a.parent:
                text = a.parent.get_text(strip=True)[:200]

            nm = re.search(r"(?:Chapter|Ch\.?|الفصل|فصل)\s*(\d+(?:\.\d+)?)", text, re.I)
            if not nm:
                nm = re.search(r"(\d+(?:\.\d+)?)", text)
            if nm:
                try:
                    num = float(nm.group(1))
                    if 0 < num < 9999 and num not in chapters:
                        chapters[num] = f"{SHINIGAMI_HOME}/chapter/{uid}"
                except Exception:
                    pass

        return chapters

    # ── API endpoints ───────────────────────────────────────────────────────
    def _fetch_shngm_api(self, endpoint: str) -> Optional[dict]:
        url = f"{SHINIGAMI_API}/{endpoint.lstrip('/')}"
        try:
            r = requests.get(
                url,
                headers={
                    "Accept": "application/json",
                    "Origin": SHINIGAMI_HOME,
                    "Referer": SHINIGAMI_HOME + "/",
                    "User-Agent": self.headers.get("User-Agent", "Mozilla/5.0"),
                },
                timeout=25,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            if isinstance(data, dict) and data.get("retcode", 0) == 0:
                return data.get("data")
            return data
        except Exception:
            return None

    def _chapters_from_shngm_api(self, series_id: str) -> dict:
        data = self._fetch_shngm_api(f"chapter/{series_id}/list?page=1&page_size=9999")
        if not isinstance(data, list):
            return {}

        chapters: dict[float, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            cid = item.get("chapter_id")
            num = self._to_float(item.get("chapter_number"))
            if cid and num is not None and num not in chapters:
                chapters[num] = f"{SHINIGAMI_HOME}/chapter/{cid}"

        if chapters:
            print(f"[Shinigami] shngm API chapters: {len(chapters)}")
        return chapters

    def _images_from_shngm_api(self, chapter_id: str) -> list:
        data = self._fetch_shngm_api(f"chapter/detail/{chapter_id}")
        if not isinstance(data, dict):
            return []

        base_url = (data.get("base_url") or data.get("base_url_low") or "").rstrip("/")
        chapter = data.get("chapter") or {}
        path = chapter.get("path") or ""
        pages = chapter.get("data") or []
        if not base_url or not isinstance(pages, list):
            return []

        images = []
        for page in pages:
            if not isinstance(page, str) or not page:
                continue
            images.append(urljoin(base_url + "/", f"{path.lstrip('/')}{page}"))

        if images:
            print(f"[Shinigami] shngm API images: {len(images)}")
        return images

    def _chapters_from_api(self, series_id: str) -> dict:
        chapters: dict[float, str] = {}
        endpoints = [
            f"{SHINIGAMI_HOME}/api/series/{series_id}",
            f"{SHINIGAMI_HOME}/api/comics/{series_id}/chapters",
            f"{SHINIGAMI_HOME}/api/manga/{series_id}/chapters",
            f"{SHINIGAMI_HOME}/api/v1/series/{series_id}",
            f"{SHINIGAMI_HOME}/api/v1/chapters?series={series_id}",
        ]
        for ep in endpoints:
            try:
                raw = self._fetch(ep, accept_json=True)
                if not raw:
                    continue
                data = json.loads(raw)
                chapters = self._parse_json_chapters(data)
                if chapters:
                    print(f"[Shinigami] API {ep}: {len(chapters)} chapters")
                    return chapters
            except Exception:
                continue
        return {}

    def _chapters_from_worker(self, series_url: str) -> dict:
        if os.getenv("HF_WORKER_RUNTIME") == "1":
            return {}
        worker_url = os.getenv("HF_WORKER_URL", "").strip().rstrip("/")
        worker_key = os.getenv("HF_WORKER_KEY", "").strip() or os.getenv("WEB_PANEL_SECRET", "").strip()
        if not worker_url or not worker_key:
            return {}
        try:
            r = requests.post(
                f"{worker_url}/extract/chapters",
                json={"url": series_url},
                headers={"Authorization": f"Bearer {worker_key}"},
                timeout=40,
            )
            if r.status_code != 200:
                return {}
            data = r.json()
            chapters = data.get("chapters", {})
            out: dict[float, str] = {}
            if isinstance(chapters, dict):
                for k, v in chapters.items():
                    try:
                        num = float(k)
                        if isinstance(v, str) and v:
                            out[num] = v
                    except Exception:
                        continue
            if out:
                print(f"[Shinigami] Worker chapters: {len(out)} chapters")
            return out
        except Exception:
            return {}

    def _parse_json_chapters(self, data) -> dict:
        chapters: dict[float, str] = {}
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("chapters", "data", "chapter_list", "results", "items"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
            if not items:
                # ربما البيانات مُضمّنة في مستوى أعمق
                text = json.dumps(data)
                for m in re.finditer(rf'"id"\s*:\s*"({UUID_PATTERN})"', text):
                    uid   = m.group(1)
                    start = max(0, m.start() - 300)
                    snip  = text[start: m.end() + 300]
                    for key in ("number", "chapterNumber", "chapter", "order"):
                        nm = re.search(rf'"{key}"\s*:\s*(\d+(?:\.\d+)?)', snip)
                        if nm:
                            num = float(nm.group(1))
                            if 0 < num < 9999 and num not in chapters:
                                chapters[num] = f"{SHINIGAMI_HOME}/chapter/{uid}"
                            break
                return chapters

        for item in items:
            if not isinstance(item, dict):
                continue
            uid = (item.get("id") or item.get("chapterId") or
                   item.get("chapter_id") or item.get("slug") or "")
            num = (item.get("number") or item.get("chapter") or
                   item.get("chapterNumber") or item.get("chapter_number") or
                   item.get("order"))
            if uid and num is not None:
                try:
                    n = float(num)
                    if 0 < n < 9999 and n not in chapters:
                        chapters[n] = f"{SHINIGAMI_HOME}/chapter/{uid}"
                except Exception:
                    pass
        return chapters

    # ── واجهة get_all_chapters ──────────────────────────────────────────────
    def _sync_get_all_chapters(self, series_url: str) -> dict:
        print(f"[Shinigami] جلب الفصول: {series_url}")

        sid = self._extract_uuid(series_url, "series")
        if sid:
            chs = self._chapters_from_shngm_api(sid)
            if chs:
                return chs

        html = self._fetch(series_url)
        if html:
            chs = self._chapters_from_next_data(html)
            if chs:
                return chs
            chs = self._chapters_from_html(html, series_url)
            if chs:
                print(f"[Shinigami] HTML: {len(chs)} chapters")
                return chs

        # fallback: API
        if sid:
            chs = self._chapters_from_api(sid)
            if chs:
                return chs

        # fallback نهائي: اسأل HF Worker (IP مختلف غالباً => bypass بدون كوكي محلي)
        chs = self._chapters_from_worker(series_url)
        if chs:
            return chs

        print(f"[Shinigami] ⚠️ لم يُعثر على فصول لـ {series_url}")
        return {}

    def _images_from_worker(self, chapter_url: str) -> list:
        if os.getenv("HF_WORKER_RUNTIME") == "1":
            return []
        worker_url = os.getenv("HF_WORKER_URL", "").strip().rstrip("/")
        worker_key = os.getenv("HF_WORKER_KEY", "").strip() or os.getenv("WEB_PANEL_SECRET", "").strip()
        if not worker_url or not worker_key:
            return []
        try:
            r = requests.post(
                f"{worker_url}/extract/images",
                json={"url": chapter_url},
                headers={"Authorization": f"Bearer {worker_key}"},
                timeout=40,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            images = data.get("images", [])
            if isinstance(images, list):
                images = [x for x in images if isinstance(x, str) and x.startswith("http")]
            else:
                images = []
            if images:
                print(f"[Shinigami] Worker images: {len(images)}")
            return images
        except Exception:
            return []

    async def get_all_chapters(self, series_url: str) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_all_chapters, series_url)

    def get_latest_chapter(self, url: str) -> Optional[float]:
        chs = self._sync_get_all_chapters(url)
        return max(chs.keys()) if chs else None

    # ── get_images ──────────────────────────────────────────────────────────
    def _sync_get_images(self, chapter_url: str) -> list:
        print(f"[Shinigami] جلب صور: {chapter_url}")
        images: list[str] = []

        cid = self._extract_uuid(chapter_url, "chapter")
        if cid:
            images = self._images_from_shngm_api(cid)
            if images:
                return images

        html = self._fetch(chapter_url)
        if html:
            soup = BeautifulSoup(html, "html.parser")

            # __NEXT_DATA__
            tag = soup.find("script", id="__NEXT_DATA__")
            if tag and tag.string:
                try:
                    raw = json.loads(tag.string)
                    for item in self._walk_json(raw):
                        if isinstance(item, str):
                            img_url = self._normalize_url(item)
                            if (img_url.startswith("http") and IMG_EXT_RE.search(img_url)
                                    and img_url not in images
                                    and not any(x in img_url.lower()
                                                for x in ["logo", "avatar", "icon", "banner"])):
                                images.append(img_url)
                    if images:
                        print(f"[Shinigami] NEXT_DATA images: {len(images)}")
                        return images
                except Exception:
                    pass

            # img tags
            for img in soup.find_all("img"):
                src = (img.get("data-src") or img.get("src") or
                       img.get("data-lazy-src") or "").strip()
                if (src.startswith("http") and src not in images
                        and not any(x in src.lower()
                                    for x in ["logo", "avatar", "icon"])):
                    images.append(src)

            if images:
                print(f"[Shinigami] HTML images: {len(images)}")
                return images

        # API fallback
        if cid:
            for ep in [
                f"{SHINIGAMI_HOME}/api/chapter/{cid}",
                f"{SHINIGAMI_HOME}/api/chapters/{cid}",
                f"{SHINIGAMI_HOME}/api/v1/chapter/{cid}",
            ]:
                try:
                    raw = self._fetch(ep, accept_json=True)
                    if not raw:
                        continue
                    text = json.dumps(json.loads(raw))
                    for m in re.finditer(
                        r'"(https?://[^"]+\.(?:webp|jpg|jpeg|png)(?:[^"]{0,100})?)"', text
                    ):
                        img_url = m.group(1).replace("\\/", "/").replace("\\u002F", "/")
                        if (img_url.startswith("http") and img_url not in images
                                and not any(x in img_url.lower()
                                            for x in ["logo", "avatar", "icon"])):
                            images.append(img_url)
                    if images:
                        print(f"[Shinigami] API images: {len(images)}")
                        return images
                except Exception:
                    continue

        images = self._images_from_worker(chapter_url)
        if images:
            return images

        print(f"[Shinigami] ⚠️ لم يُعثر على صور لـ {chapter_url}")
        return images

    async def get_images(self, chapter_url: str) -> list:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_get_images, chapter_url)
