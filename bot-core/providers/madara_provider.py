import re
import json
from bs4 import BeautifulSoup
from .base_provider import BaseProvider
from urllib.parse import urljoin, urlparse


class MadaraProvider(BaseProvider):
    """
    مزود عائلة Madara WordPress + Next.js.
    يدعم: AJAX chapters، Pagination (?page=N / /page/N/)، و Next.js data.
    """

    def __init__(self, scraper=None):
        super().__init__(scraper)

    # ── صور الفصل ─────────────────────────────────────────────────────────
    async def get_images(self, url: str):
        try:
            html = self.fetch_html(url, {'Referer': url})
            if not html:
                return []
            soup   = BeautifulSoup(html, 'html.parser')
            images = []

            # 0. ts_reader.run JSON (MangaReader / MangaStream / TS Reader WordPress themes)
            m_ts = re.search(r'ts_reader\.run\((\{.*?\})\);', html, re.DOTALL)
            if m_ts:
                try:
                    ts_data = json.loads(m_ts.group(1))
                    for src_obj in ts_data.get("sources", []):
                        for img in src_obj.get("images", []):
                            img_clean = str(img).replace(r'\/', '/').replace('\\', '').strip()
                            if img_clean.startswith('http') and img_clean not in images:
                                if not any(x in img_clean.lower() for x in ['logo', 'banner', 'icon', 'avatar', 'readerarea.svg']):
                                    images.append(img_clean)
                    if len(images) >= 3:
                        return images
                except Exception:
                    pass

            # 0b. General "sources": [{"images": [...]}] in JSON
            m_sources = re.search(r'"sources"\s*:\s*(\[\s*\{.*?\}\s*\])', html, re.DOTALL)
            if m_sources:
                try:
                    sources = json.loads(m_sources.group(1))
                    for src_obj in sources:
                        for img in src_obj.get("images", []):
                            img_clean = str(img).replace(r'\/', '/').replace('\\', '').strip()
                            if img_clean.startswith('http') and img_clean not in images:
                                if not any(x in img_clean.lower() for x in ['logo', 'banner', 'icon', 'avatar', 'readerarea.svg']):
                                    images.append(img_clean)
                    if len(images) >= 3:
                        return images
                except Exception:
                    pass

            # 1. __NEXT_DATA__
            nd = soup.find('script', id='__NEXT_DATA__')
            if nd:
                try:
                    images = self._regex_images(json.dumps(json.loads(nd.string)))
                    if len(images) >= 3:
                        return images
                except Exception:
                    pass

            # 2. سلكتورات Madara + selectors إضافية للمرونة
            for sel in [
                "div.reading-content img", "div.page-break img",
                "div#chapter-images img", "div.wp-manga-chapter-img img",
                "img.wp-manga-chapter-img", "div.chapter-images img",
                "div.read-container img", "div.viewer-images img",
                "div#reader-images img",
                "div.comic-images-wrapper img", "div.reader-mode-strip img",
                "figure.image-container img", "img[data-src]",
            ]:
                for img in soup.select(sel):
                    src = (img.get('data-src') or img.get('data-lazy-src') or
                           img.get('data-cfsrc') or img.get('src') or '').strip()
                    if not src:
                        continue
                    if src.startswith('//'):
                        src = 'https:' + src
                    elif not src.startswith('http'):
                        src = urljoin(url, src)
                    if src not in images and not (
                        any(x in src.lower() for x in ['logo', 'banner', 'icon', 'avatar', 'readerarea.svg'])
                        or re.search(r'\bads\b', src.lower())
                    ):
                        images.append(src)
                if len(images) >= 3:
                    return images

            # 3. scripts regex
            for script in soup.find_all('script'):
                content = script.string or ''
                if any(x in content for x in ['.webp', '.jpg', '.jpeg', '.png']):
                    for f in self._regex_images(content):
                        if f not in images:
                            images.append(f)
                if len(images) >= 3:
                    return images

            # 4. Fallback: Full HTML regex (دمج بدون تكرار)
            for f in self._regex_images(html):
                if f not in images:
                    images.append(f)

            return images
        except Exception as e:
            print(f"[Madara] get_images: {e}")
            return []

    def _regex_images(self, text: str) -> list:
        images = []
        for pattern in [
            r'"src"\s*:\s*"(https?:[\\/]+[^"]+\.(?:webp|jpg|jpeg|png)[^"]*)"',
            r'"url"\s*:\s*"(https?:[\\/]+[^"]+\.(?:webp|jpg|jpeg|png)[^"]*)"',
            r'https?:(?:\\/|/){2}[a-zA-Z0-9\-_.]+(?:\\/|/)[^"\'\s<>]+?\.(?:webp|jpg|jpeg|png)(?:\?[^"\'\s<>]*)?',
        ]:
            for m in re.findall(pattern, text, re.IGNORECASE):
                src = (m if isinstance(m, str) else m[0])
                src = src.replace('\\u002F', '/').replace('\\/', '/').replace('\\', '').strip().rstrip('"')
                if src.startswith('http') and src not in images:
                    if not (
                        any(x in src.lower() for x in ['logo', 'avatar', 'icon', 'banner', 'cover', 'readerarea.svg'])
                        or re.search(r'\bads\b', src.lower())
                    ):
                        images.append(src)
        return images

    # ── كل الفصول ─────────────────────────────────────────────────────────
    async def get_all_chapters(self, series_url: str) -> dict:
        try:
            html = self.fetch_html(series_url)
            if not html:
                return {}
            soup = BeautifulSoup(html, 'html.parser')

            # 1. Next.js data
            nd = soup.find('script', id='__NEXT_DATA__')
            if nd:
                try:
                    chs = self._from_next(json.dumps(json.loads(nd.string)), series_url)
                    if chs:
                        return chs
                except Exception:
                    pass

            # 2. HTML directo
            chs = self._from_html(soup, series_url)

            # 3. Madara AJAX (يجلب كل الفصول دفعة واحدة)
            if not chs:
                chs = await self._ajax_chapters(soup, series_url)

            # 4. Pagination — لو وجدنا فصولاً قليلة (< 30) جرب صفحات إضافية
            if len(chs) < 30:
                extra = self._paginate_chapters(
                    series_url,
                    lambda h, u: self._from_html(BeautifulSoup(h, 'html.parser'), u),
                )
                chs.update(extra)

            return chs
        except Exception as e:
            print(f"[Madara] get_all_chapters: {e}")
            return {}

    def _from_next(self, text: str, series_url: str) -> dict:
        chs    = {}
        parsed = urlparse(series_url)
        base   = f"{parsed.scheme}://{parsed.netloc}"
        for m in re.finditer(
            r'"((?:https?://[^"]+|/[^"]+)/(?:chapter[s]?|ch)[/-](\d+(?:\.\d+)?)(?:[/"\\]))', text
        ):
            href = m.group(1).replace('\\u002F', '/').replace('\\', '')
            num  = float(m.group(2))
            if not href.startswith('http'):
                href = urljoin(base, href)
            chs[num] = href
        return chs

    def _from_html(self, soup, series_url: str) -> dict:
        chs = {}
        for sel in [
            "li.wp-manga-chapter a", "div#chapterlist li a",
            "ul.main.version-chap li a", "div.eph-num a",
            "a[href*='/chapter']", "a[href*='chapter-']",
        ]:
            for a in soup.select(sel):
                href = a.get('href', '').strip()
                if not href:
                    continue
                if not href.startswith('http'):
                    href = urljoin(series_url, href)
                m = (re.search(r'chapter\s*([\d.]+)', a.get_text().lower())
                     or re.search(r'(?:chapter[s]?|ch)[/-]([\d.]+)', href, re.I))
                if m:
                    try:
                        chs[float(m.group(1))] = href
                    except Exception:
                        pass
            if chs:
                break
        return chs

    async def _ajax_chapters(self, soup, series_url: str) -> dict:
        """
        Madara AJAX: POST /wp-admin/admin-ajax.php
        action=manga_get_chapters&manga={post_id}
        بعض المواقع تُرجع كل الفصول مرة واحدة، بعضها يُرجع بالصفحات.
        """
        try:
            holder  = soup.select_one("#manga-chapters-holder")
            post_id = holder.get("data-id") if holder else None
            if not post_id:
                m = re.search(r'manga_id\s*:\s*(\d+)', str(soup))
                if m:
                    post_id = m.group(1)
            if not post_id:
                return {}

            base     = "/".join(series_url.split("/")[:3])
            ajax_url = f"{base}/wp-admin/admin-ajax.php"
            all_chs  = {}

            # بعض المواقع تدعم pagination في AJAX (paged=N)
            for page_num in range(1, 100):
                payload = {
                    "action": "manga_get_chapters",
                    "manga":  post_id,
                    "paged":  str(page_num),
                }
                try:
                    resp = self.scraper.post(ajax_url, data=payload,
                                             headers=self.headers, timeout=15)
                    if resp.status_code != 200 or not resp.text.strip() or resp.text.strip() in ("0", "false", ""):
                        break
                    ajax_soup = BeautifulSoup(resp.text, 'html.parser')
                    page_chs  = self._from_html(ajax_soup, series_url)
                    if not page_chs:
                        break
                    before = len(all_chs)
                    all_chs.update(page_chs)
                    if len(all_chs) == before:
                        break      # لا فصول جديدة = انتهى
                    if page_num == 1 and len(page_chs) > 50:
                        break      # صفحة واحدة كافية إذا أرجعت كثيراً
                except Exception:
                    break

            return all_chs
        except Exception as e:
            print(f"[Madara] AJAX: {e}")
            return {}

    def get_latest_chapter(self, url: str):
        import asyncio
        loop   = asyncio.new_event_loop()
        result = loop.run_until_complete(self.get_all_chapters(url))
        loop.close()
        return max(result.keys()) if result else None
