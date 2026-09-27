import re
import logging
import aiohttp
import asyncio
from typing import Optional, List, Dict, Any
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

logger = logging.getLogger("mangasystem.feed_watcher")

class FeedWatcher:
    """
    نظام مراقبة التحديثات العامة والخاصة عبر الـ RSS والـ APIs للمواقع الكبرى.
    يسرّع عملية رصد الفصول الجديدة ليصبح شبه لحظي.
    """

    # روابط الفيد العام الأحدث
    GLOBAL_FEEDS = {
        "mangadex": "https://api.mangadex.org/chapter?limit=100&order[publishAt]=desc&translatedLanguage[]=en&translatedLanguage[]=ar",
        "comick": "https://api.comick.fun/chapter?limit=50&order=new",
        "asura": "https://asuracomic.net/feed/",
        "shinigami": "https://g.shinigami.asia/feed/",
        "lekmanga": "https://lekmanga.net/feed/",
    }

    def __init__(self, provider_manager=None):
        self.provider_manager = provider_manager


    def detect_check_method(self, url: str) -> str:
        """تحديد طريقة الفحص الأسرع للرابط تلقائياً"""
        domain = urlparse(url).netloc.lower()
        if "mangadex.org" in domain:
            return "mangadex_api"
        elif any(x in domain for x in ("comick.fun", "comick.io", "comick.cc")):
            return "comick_api"
        elif "webtoons.com" in domain:
            return "rss"
        elif "feed" in url.lower() or url.endswith(".xml") or url.endswith(".rss"):
            return "rss"

        # Check using provider manager to detect WordPress/Madara sites
        is_wordpress = False
        if self.provider_manager:
            try:
                p = self.provider_manager.get_provider(url)
                if p:
                    class_name = p.__class__.__name__
                    if class_name in ("MadaraProvider", "AsuraProvider", "ShinigamiProvider", "LekMangaProvider"):
                        is_wordpress = True
            except Exception:
                pass
        else:
            # Simple domain fallback if provider manager is not passed
            if any(x in domain for x in ("asuracomic.net", "asurascans.com", "asuratoon.com", "shinigami", "shngm", "lekmanga")):
                is_wordpress = True

        if is_wordpress:
            return "rss"

        return "scrape"



    async def quick_check_individual(self, url: str, last_chapter: float, check_method: str = "auto") -> Optional[Dict[str, Any]]:
        """
        فحص سريع ومحدد لتراكر واحد باستخدام الـ API أو الـ RSS المباشر
        لتجنب تحميل الصفحة كاملة (Scraping)
        """
        if check_method == "auto":
            check_method = self.detect_check_method(url)

        try:
            if check_method == "mangadex_api":
                return await self.check_mangadex_individual(url, last_chapter)
            elif check_method == "comick_api":
                return await self.check_comick_individual(url, last_chapter)
            elif check_method == "rss" or "webtoons.com" in url:
                return await self.check_rss_individual(url, last_chapter)
        except Exception as e:
            logger.error(f"[FeedWatcher] Error checking individual feed for {url}: {e}")
        return None

    # ── MangaDex Individual API Check ─────────────────────────────────────────
    async def check_mangadex_individual(self, series_url: str, last_ch: float) -> Optional[Dict[str, Any]]:
        m = re.search(r'/(?:manga|title)/([0-9a-f-]{36})', series_url)
        if not m:
            return None
        manga_id = m.group(1)
        api_url = f"https://api.mangadex.org/manga/{manga_id}/feed"
        params = {
            "limit": 10,
            "order[chapter]": "desc",
            "translatedLanguage[]": ["en", "ar"],
            "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"],
        }
        
        start_time = asyncio.get_running_loop().time()
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                check_ms = int((asyncio.get_running_loop().time() - start_time) * 1000)
                if r.status != 200:
                    return None
                data = await r.json()

        chapters = data.get("data", [])
        if not chapters:
            return None

        new_chapters = []
        latest = last_ch

        for ch in chapters:
            ch_num_str = ch["attributes"].get("chapter")
            ch_id = ch["id"]
            if not ch_num_str:
                continue
            try:
                ch_num = float(ch_num_str)
                ch_url = f"https://mangadex.org/chapter/{ch_id}"
                if ch_num > last_ch:
                    new_chapters.append({
                        "num": ch_num,
                        "url": ch_url,
                        "locked": False
                    })
                    if ch_num > latest:
                        latest = ch_num
            except ValueError:
                continue

        if new_chapters:
            # ترتيب الفصول تصاعدياً
            new_chapters.sort(key=lambda x: x["num"])
            return {
                "latest": latest,
                "new_chapters": new_chapters,
                "check_ms": check_ms
            }
        return None

    # ── Comick Individual API Check ───────────────────────────────────────────
    async def check_comick_individual(self, series_url: str, last_ch: float) -> Optional[Dict[str, Any]]:
        m = re.search(r'/comic/([^/?#]+)', series_url)
        if not m:
            return None
        slug = m.group(1)
        
        start_time = asyncio.get_running_loop().time()
        async with aiohttp.ClientSession() as session:
            # 1. جلب الـ hid للعمل
            async with session.get(f"https://api.comick.fun/comic/{slug}", timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                comic_data = await r.json()
            hid = comic_data.get("comic", {}).get("hid")
            if not hid:
                return None

            # 2. جلب آخر فصول العمل
            params = {
                "lang": "en,ar",
                "limit": 10,
                "order": "desc",
            }
            async with session.get(f"https://api.comick.fun/comic/{hid}/chapters", params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                check_ms = int((asyncio.get_running_loop().time() - start_time) * 1000)
                if r.status != 200:
                    return None
                data = await r.json()

        chapters = data.get("chapters", [])
        if not chapters:
            return None

        new_chapters = []
        latest = last_ch

        for ch in chapters:
            ch_num = ch.get("chap")
            ch_hid = ch.get("hid")
            if not ch_num or not ch_hid:
                continue
            try:
                num = float(ch_num)
                ch_url = f"https://comick.fun/comic/{slug}/{ch_hid}-chapter-{ch_num}-en"
                if num > last_ch:
                    new_chapters.append({
                        "num": num,
                        "url": ch_url,
                        "locked": False
                    })
                    if num > latest:
                        latest = num
            except ValueError:
                continue

        if new_chapters:
            new_chapters.sort(key=lambda x: x["num"])
            return {
                "latest": latest,
                "new_chapters": new_chapters,
                "check_ms": check_ms
            }
        return None

    async def check_rss_individual(self, url: str, last_ch: float) -> Optional[Dict[str, Any]]:
        # بناء رابط فيد RSS لمواقع معينة
        feed_url = url
        
        is_wordpress = False
        if self.provider_manager:
            try:
                p = self.provider_manager.get_provider(url)
                if p and p.__class__.__name__ in ("MadaraProvider", "AsuraProvider", "ShinigamiProvider", "LekMangaProvider"):
                    is_wordpress = True
            except Exception:
                pass
        else:
            # Simple domain fallback if provider manager is not passed
            domain = urlparse(url).netloc.lower()
            if any(x in domain for x in ("asuracomic.net", "asurascans.com", "asuratoon.com", "shinigami", "shngm", "lekmanga")):
                is_wordpress = True

        if is_wordpress:
            feed_url = f"{url.rstrip('/')}/feed/"
        elif "webtoons.com" in url:
            m = re.search(r'titleNo=(\d+)', url)
            if m:
                feed_url = f"https://www.webtoons.com/rss?titleNo={m.group(1)}"


        start_time = asyncio.get_running_loop().time()
        async with aiohttp.ClientSession() as session:
            async with session.get(feed_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                check_ms = int((asyncio.get_running_loop().time() - start_time) * 1000)
                if r.status != 200:
                    return None
                xml_text = await r.text()

        new_chapters = self.parse_rss_chapters(xml_text, last_ch)
        if new_chapters:
            new_chapters.sort(key=lambda x: x["num"])
            return {
                "latest": max(x["num"] for x in new_chapters),
                "new_chapters": new_chapters,
                "check_ms": check_ms
            }
        return None

    def parse_rss_chapters(self, xml_text: str, last_ch: float) -> List[Dict[str, Any]]:
        """تحليل محتوى RSS XML واستخراج الفصول الجديدة"""
        new_chapters = []
        try:
            root = ET.fromstring(xml_text)
            # معالجة فروقات Namespace
            ns = {}
            if "http://www.w3.org/2005/Atom" in root.tag:
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                items = root.findall(".//atom:entry", ns)
            else:
                items = root.findall(".//item")

            for item in items:
                title_el = item.find("title") if not ns else item.find("atom:title", ns)
                link_el = item.find("link") if not ns else item.find("atom:link", ns)
                
                title = title_el.text if title_el is not None else ""
                link = ""
                if link_el is not None:
                    if not ns:
                        link = link_el.text or ""
                    else:
                        link = link_el.attrib.get("href", "")

                if not title or not link:
                    continue

                # محاولة استخراج رقم الفصل من العنوان
                ch_num = self.extract_chapter_number_from_text(title)
                if ch_num is None:
                    # محاولة استخراج رقم الفصل من الرابط
                    ch_num = self.extract_chapter_number_from_text(link)

                if ch_num is not None and ch_num > last_ch:
                    new_chapters.append({
                        "num": ch_num,
                        "url": link,
                        "locked": False
                    })
        except Exception as e:
            logger.error(f"[FeedWatcher] Failed to parse RSS XML: {e}")
            
            # Fallback regex parsing if XML fails
            try:
                links = re.findall(r'<link[^>]*>(.*?)</link>', xml_text)
                titles = re.findall(r'<title[^>]*>(.*?)</title>', xml_text)
                for t, l in zip(titles, links):
                    ch_num = self.extract_chapter_number_from_text(t)
                    if ch_num is None:
                        ch_num = self.extract_chapter_number_from_text(l)
                    if ch_num is not None and ch_num > last_ch:
                        new_chapters.append({
                            "num": ch_num,
                            "url": l.strip(),
                            "locked": False
                        })
            except Exception as e2:
                logger.error(f"[FeedWatcher] Regex RSS fallback also failed: {e2}")

        return new_chapters

    def extract_chapter_number_from_text(self, text: str) -> Optional[float]:
        """استخراج رقم الفصل من النص"""
        if not text:
            return None
        text = text.strip()
        # أنماط شائعة للفصول
        patterns = [
            r"(?i)(?:الفصل|فصل|chapter|ch|ep|v)\s*[:\-]?\s*(\d+(?:\.\d+)?)",
            r"chapter[-/](\d+(?:\.\d+)?)",
            r"(\d+(?:\.\d+)?)\s*(?:话|章|集|回|册)",
            r"^\s*(\d+(?:\.\d+)?)",
        ]
        for p in patterns:
            m = re.search(p, text)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    pass
        return None

    # ── Global Feeds Watcher (The core real-time enhancer) ────────────────────
    async def poll_global_feeds(self, tracked_trackers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        فحص التحديثات العامة للمواقع الكبرى ومطابقتها مع التراكرز المتتبعة في البوت.
        tracked_trackers: قائمة ببيانات المتابعات النشطة.
        """
        results = []
        
        # 1. فحص التغذية لـ MangaDex
        try:
            md_updates = await self.check_mangadex_global()
            if md_updates:
                results.extend(self.match_global_updates(md_updates, tracked_trackers, "mangadex"))
        except Exception as e:
            logger.error(f"[FeedWatcher] MangaDex global poll error: {e}")

        # 2. فحص التغذية لـ Comick
        try:
            comick_updates = await self.check_comick_global()
            if comick_updates:
                results.extend(self.match_global_updates(comick_updates, tracked_trackers, "comick"))
        except Exception as e:
            logger.error(f"[FeedWatcher] Comick global poll error: {e}")

        # 3. فحص التغذية لـ Asura Scans
        try:
            asura_updates = await self.check_wordpress_global_rss("asura")
            if asura_updates:
                results.extend(self.match_global_updates(asura_updates, tracked_trackers, "asura"))
        except Exception as e:
            logger.error(f"[FeedWatcher] Asura global RSS poll error: {e}")

        # 4. فحص التغذية لـ Shinigami
        try:
            shinigami_updates = await self.check_wordpress_global_rss("shinigami")
            if shinigami_updates:
                results.extend(self.match_global_updates(shinigami_updates, tracked_trackers, "shinigami"))
        except Exception as e:
            logger.error(f"[FeedWatcher] Shinigami global RSS poll error: {e}")

        # 5. فحص التغذية لـ Lekmanga
        try:
            lekmanga_updates = await self.check_wordpress_global_rss("lekmanga")
            if lekmanga_updates:
                results.extend(self.match_global_updates(lekmanga_updates, tracked_trackers, "lekmanga"))
        except Exception as e:
            logger.error(f"[FeedWatcher] Lekmanga global RSS poll error: {e}")

        return results


    async def check_mangadex_global(self) -> List[Dict[str, Any]]:
        """فحص أحدث 100 فصل تم رفعهم عالمياً على MangaDex"""
        async with aiohttp.ClientSession() as session:
            async with session.get(self.GLOBAL_FEEDS["mangadex"], timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return []
                data = await r.json()

        updates = []
        for ch in data.get("data", []):
            ch_num_str = ch["attributes"].get("chapter")
            ch_id = ch["id"]
            
            # استخراج معرف المانجا
            manga_id = None
            for rel in ch.get("relationships", []):
                if rel.get("type") == "manga":
                    manga_id = rel.get("id")
                    break

            if not ch_num_str or not manga_id:
                continue

            try:
                ch_num = float(ch_num_str)
                updates.append({
                    "series_key": manga_id,  # معرف المانجا لمطابقته
                    "num": ch_num,
                    "url": f"https://mangadex.org/chapter/{ch_id}",
                    "locked": False
                })
            except ValueError:
                continue
        return updates

    async def check_comick_global(self) -> List[Dict[str, Any]]:
        """فحص أحدث الفصول المرفوعة عالمياً على Comick"""
        async with aiohttp.ClientSession() as session:
            async with session.get(self.GLOBAL_FEEDS["comick"], timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return []
                data = await r.json()

        updates = []
        for ch in data:
            if not isinstance(ch, dict):
                continue
            comic = ch.get("comic") or {}
            slug = comic.get("slug")
            hid = comic.get("hid")
            ch_num = ch.get("chap")
            ch_hid = ch.get("hid")

            if not ch_num or not ch_hid or not (slug or hid):
                continue

            try:
                num = float(ch_num)
                updates.append({
                    "series_key": hid,  # يمكن المطابقة عبر الـ hid
                    "series_slug": slug, # أو عبر الـ slug
                    "num": num,
                    "url": f"https://comick.fun/comic/{slug}/{ch_hid}-chapter-{ch_num}-en",
                    "locked": False
                })
            except ValueError:
                continue
        return updates

    async def check_wordpress_global_rss(self, provider_key: str) -> List[Dict[str, Any]]:
        """فحص الـ RSS feed العام لموقع ووردبريس"""
        url = self.GLOBAL_FEEDS.get(provider_key)
        if not url:
            return []
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return []
                xml_text = await r.text()

        updates = []
        try:
            root = ET.fromstring(xml_text)
            for item in root.findall(".//item"):
                title_el = item.find("title")
                link_el = item.find("link")
                title = title_el.text if title_el is not None else ""
                link = link_el.text if link_el is not None else ""
                
                if not title or not link:
                    continue

                ch_num = self.extract_chapter_number_from_text(title)
                if ch_num is None:
                    ch_num = self.extract_chapter_number_from_text(link)

                if ch_num is not None:
                    # استخراج الـ slug للعمل من رابط الفصل
                    # مثلاً: https://asuracomic.net/series/solo-leveling/chapter-150 -> solo-leveling
                    slug = self.extract_slug_from_chapter_url(link)
                    if slug:
                        updates.append({
                            "series_slug": slug,
                            "num": ch_num,
                            "url": link,
                            "locked": False
                        })
        except Exception as e:
            logger.error(f"[FeedWatcher] Error parsing WordPress global RSS for {provider_key}: {e}")
            
            # Fallback regex
            try:
                links = re.findall(r'<link[^>]*>(.*?)</link>', xml_text)
                titles = re.findall(r'<title[^>]*>(.*?)</title>', xml_text)
                for t, l in zip(titles, links):
                    ch_num = self.extract_chapter_number_from_text(t)
                    if ch_num is None:
                        ch_num = self.extract_chapter_number_from_text(l)
                    if ch_num is not None:
                        slug = self.extract_slug_from_chapter_url(l)
                        if slug:
                            updates.append({
                                "series_slug": slug,
                                "num": ch_num,
                                "url": l.strip(),
                                "locked": False
                            })
            except Exception:
                pass

        return updates

    def extract_slug_from_chapter_url(self, url: str) -> Optional[str]:
        """استخراج المعرف أو الـ slug للمانجا من رابط الفصل لعمل مطابقة صحيحة"""
        url_path = urlparse(url).path
        # Asura Scans shape: /series/solo-leveling-chapter-150 or /comics/solo-leveling/chapter/150
        # Shinigami shape: /chapter/uuid or /series/uuid
        # Let's extract any parts
        parts = [p for p in url_path.split("/") if p]
        if not parts:
            return None
        
        # استبعاد كلمة 'chapter' و 'series' للوصول للاسم الفعلي
        for word in ("chapter", "series", "comic", "manga", "comics"):
            if word in parts:
                idx = parts.index(word)
                if idx + 1 < len(parts):
                    return parts[idx + 1]
                if idx - 1 >= 0:
                    return parts[idx - 1]
        
        # إذا لم نجد الكلمات المفتاحية، نرجع أول جزء منطقي من الرابط
        return parts[0]

    def match_global_updates(self, updates: List[Dict[str, Any]], trackers: List[Dict[str, Any]], provider: str) -> List[Dict[str, Any]]:
        """مطابقة التحديثات العالمية المكتشفة مع التراكرز المسجلة بالبوت"""
        matches = []
        for tr in trackers:
            tr_url = tr.get("url", "")
            last_ch = tr.get("last_chapter", 0.0)
            
            # مطابقة الدومين
            domain = urlparse(tr_url).netloc.lower()
            if provider == "mangadex" and "mangadex.org" not in domain:
                continue
            if provider == "comick" and not any(x in domain for x in ("comick.fun", "comick.io", "comick.cc")):
                continue
            if provider == "asura" and not any(x in domain for x in ("asuracomic.net", "asurascans.com", "asuratoon.com")):
                continue
            if provider == "shinigami" and not any(x in domain for x in ("shinigami.asia", "shngm.io", "shinigami.id")):
                continue
            if provider == "lekmanga" and not any(x in domain for x in ("lekmanga.net", "lekmanga.org")):
                continue


            for up in updates:
                is_matched = False
                
                if provider == "mangadex":
                    # المطابقة عبر الـ uuid
                    m = re.search(r'/(?:manga|title)/([0-9a-f-]{36})', tr_url)
                    if m and m.group(1) == up.get("series_key"):
                        is_matched = True
                        
                elif provider == "comick":
                    # المطابقة عبر الـ hid أو الـ slug
                    # Comick API return both
                    m_slug = re.search(r'/comic/([^/?#]+)', tr_url)
                    if m_slug:
                        tr_slug = m_slug.group(1)
                        if tr_slug == up.get("series_slug") or tr_slug == up.get("series_key"):
                            is_matched = True

                else:
                    # مواقع WordPress: المطابقة عبر الـ slug المستخرج من الرابط
                    tr_slug = self.extract_slug_from_chapter_url(tr_url)
                    up_slug = up.get("series_slug")
                    if tr_slug and up_slug and (tr_slug == up_slug or tr_slug in up_slug or up_slug in tr_slug):
                        is_matched = True

                if is_matched and up["num"] > last_ch:
                    # رصد التحديث!
                    match_item = tr.copy()
                    match_item.update({
                        "new_chapter": up["num"],
                        "chapter_url": up["url"],
                        "locked": up["locked"],
                        "matched_via": "global_feed"
                    })
                    # تجنب تكرار الفصول المطابقة لنفس التراكر
                    if not any(m["tracker_id"] == match_item["tracker_id"] and m["new_chapter"] == match_item["new_chapter"] for m in matches):
                        matches.append(match_item)
                        logger.info(f"[FeedWatcher] Global match found! {tr.get('title')} Ch.{up['num']} ({provider})")

        return matches
