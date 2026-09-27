from .base_provider import BaseProvider
from .suwayomi_client import SuwayomiClient
from urllib.parse import urlparse, urljoin
import logging
import asyncio
import re
import os
import aiohttp

logger = logging.getLogger("SuwayomiProvider")

class SuwayomiProvider(BaseProvider):
    def __init__(self):
        super().__init__()
        self.client = SuwayomiClient()
        # خريطة نطاقات المواقع إلى أسماء المصادر المقابلة في Suwayomi
        self.domain_to_source_mapping = {
            "olympustaff.com": "Olympus Scanlation",
            "olympusxyz.com": "Olympus Scanlation",
            "olympusscanlation.com": "Olympus Scanlation",
            "azoramoon.com": "Azora Moon",
            "mangabuff.ru": "MangaBuff",
            "flamecomics.com": "Flame Comics",
            "flamecomics.xyz": "Flame Comics",
            "asuracomic.net": "Asura Scans",
            "asurascans.com": "Asura Scans",
        }

    def _normalize_domain(self, url: str) -> str:
        try:
            parsed = urlparse(url)
            host = parsed.netloc or url
            return host.lower().replace("www.", "").strip()
        except Exception:
            return url.split("/")[0].lower()

    async def get_source_by_domain(self, domain: str, retry_on_failure: bool = True) -> tuple[str, str]:
        """
        تحديد معرف المصدر (sourceId) واسم حزمة الإضافة (pkgName) المتوافقة مع النطاق المعين.
        تقوم هذه الدالة بتثبيت الإضافة تلقائياً في حال لم تكن مثبتة.
        """
        # 1. البحث في الخريطة الثابتة أولاً
        clean_name = self.domain_to_source_mapping.get(domain)
        
        # 2. إذا لم توجد، تخمين اسم المصدر من النطاق نفسه
        if not clean_name:
            clean_name = domain.split(".")[0].lower()

        # 3. التحقق من صحة اتصال خادم Suwayomi
        is_healthy = await self.client.check_health()
        if not is_healthy:
            logger.error("Suwayomi-Server is not healthy or unreachable.")
            return None, None

        # 4. التأكد من تسجيل مستودع Keiyoushi كـ Extension Repo
        await self.client.add_keiyoushi_repo()

        # 5. تحديث قائمة الإضافات المتاحة
        logger.info("Fetching Suwayomi extensions...")
        await self.client.fetch_extensions_list()

        # 6. جلب قائمة الإضافات والمصادر ومطابقتها
        exts = await self.client.get_extensions()
        
        target_ext = None
        target_source_id = None
        
        # البحث عن مصدر يحتوي اسمه على الكلمة المفتاحية
        for ext in exts:
            sources = ext.get("source", {}).get("nodes", [])
            for src in sources:
                src_name = src.get("name", "").lower()
                # مطابقة الاسم
                if clean_name.lower() in src_name or src_name.replace(" ", "") in domain.replace(".", ""):
                    target_ext = ext
                    target_source_id = src["id"]
                    break
            if target_ext:
                break

        # بحث بديل واسع المدى في حال فشل المطابقة الأولى
        if not target_ext:
            for ext in exts:
                if clean_name.lower() in ext.get("name", "").lower() or clean_name.lower() in ext.get("pkgName", "").lower():
                    target_ext = ext
                    sources = ext.get("source", {}).get("nodes", [])
                    if sources:
                        target_source_id = sources[0]["id"]
                    break

        if not target_ext:
            if retry_on_failure:
                logger.info(f"Source not found for domain {domain}. Triggering extension update maintenance...")
                await self.client.maintenance(force_update=True)
                return await self.get_source_by_domain(domain, retry_on_failure=False)
            logger.error(f"Could not find any Suwayomi extension matching domain {domain}")
            return None, None

        # 7. التثبيت التلقائي للإضافة إذا كانت غير مثبتة
        if not target_ext.get("isInstalled", False):
            logger.info(f"Installing Suwayomi extension: {target_ext['name']} ({target_ext['pkgName']})...")
            success = await self.client.install_or_update_extension(target_ext["pkgName"], install=True)
            if not success:
                if retry_on_failure:
                    logger.info(f"Failed to install {target_ext['pkgName']}. Triggering maintenance and retrying...")
                    await self.client.maintenance(force_update=True)
                    return await self.get_source_by_domain(domain, retry_on_failure=False)
                logger.error(f"Failed to install extension {target_ext['pkgName']}")
                return None, None
            
            # الانتظار حتى اكتمال التثبيت
            for _ in range(12):
                await asyncio.sleep(2)
                cur_exts = await self.client.get_extensions()
                for e in cur_exts:
                    if e["pkgName"] == target_ext["pkgName"] and e.get("isInstalled", False):
                        logger.info("Extension installed successfully!")
                        src_nodes = e.get("source", {}).get("nodes", [])
                        if src_nodes:
                            target_source_id = src_nodes[0]["id"]
                        break
                else:
                    continue
                break
        
        return target_source_id, target_ext["pkgName"]

    async def _worker_extract_chapters(self, series_url: str) -> dict[float, str]:
        worker_url = os.getenv("HF_WORKER_URL", "").strip().rstrip("/")
        worker_key = os.getenv("HF_WORKER_KEY", "").strip() or os.getenv("WEB_PANEL_SECRET", "").strip()
        if not worker_url or not worker_key:
            logger.warning("HF Worker URL or Key not configured; cannot delegate Suwayomi check.")
            return {}
        try:
            headers = {"Authorization": f"Bearer {worker_key}"}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{worker_url}/extract/chapters",
                    json={"url": series_url},
                    headers=headers,
                    timeout=50
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        chapters = data.get("chapters", {})
                        result = {}
                        if isinstance(chapters, dict):
                            for k, v in chapters.items():
                                try:
                                    result[float(k)] = v
                                except ValueError:
                                    pass
                        return result
        except Exception as e:
            logger.error(f"Worker extract chapters failed: {e}")
        return {}

    async def _worker_extract_images(self, chapter_url: str) -> list[str]:
        worker_url = os.getenv("HF_WORKER_URL", "").strip().rstrip("/")
        worker_key = os.getenv("HF_WORKER_KEY", "").strip() or os.getenv("WEB_PANEL_SECRET", "").strip()
        if not worker_url or not worker_key:
            logger.warning("HF Worker URL or Key not configured; cannot delegate Suwayomi download.")
            return []
        try:
            headers = {"Authorization": f"Bearer {worker_key}"}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{worker_url}/extract/images",
                    json={"url": chapter_url},
                    headers=headers,
                    timeout=50
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        images = data.get("images", [])
                        if isinstance(images, list):
                            return [x for x in images if isinstance(x, str) and x.startswith("http")]
        except Exception as e:
            logger.error(f"Worker extract images failed: {e}")
        return []

    async def get_images(self, url: str, retry_on_failure: bool = True) -> list[str]:
        if os.getenv("HF_WORKER_RUNTIME") != "1" and os.getenv("HF_WORKER_URL"):
            logger.info(f"Delegating Suwayomi get_images to HF Worker: {url}")
            return await self._worker_extract_images(url)

        domain = self._normalize_domain(url)
        source_id, _ = await self.get_source_by_domain(domain, retry_on_failure=retry_on_failure)
        if not source_id:
            logger.error(f"Cannot get source ID for url: {url}")
            return []

        # استخراج الرابط النسبي للمانجا والرابط النسبي للفصل
        parsed = urlparse(url)
        path = parsed.path
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            logger.error(f"Invalid chapter URL pattern: {url}")
            return []

        # نفترض أن رابط السلسلة هو كل شيء ما عدا الجزء الأخير الخاص بالفصل
        manga_path = "/" + "/".join(parts[:-1])
        chapter_path = path

        logger.info(f"Importing manga {manga_path} from source {source_id}...")
        manga_id = await self.client.import_manga(source_id, manga_path)
        if not manga_id:
            # حل بديل: البحث باستخدام اسم المانجا المخمن من الرابط
            manga_title = parts[-2].replace("-", " ").title()
            logger.info(f"Manga import failed. Searching by title fallback: {manga_title}...")
            search_results = await self.client.search_manga_on_source(source_id, manga_title)
            if search_results:
                manga_id = search_results[0]["id"]

        if not manga_id:
            if retry_on_failure:
                logger.info(f"Manga import failed for {manga_path}. Triggering extension update and retrying...")
                await self.client.maintenance(force_update=True)
                return await self.get_images(url, retry_on_failure=False)
            logger.error(f"Cannot import or search manga {manga_path}")
            return []

        # جلب الفصول وتحديثها
        logger.info("Fetching chapter list from source...")
        await self.client.fetch_chapters(manga_id)
        
        # جلب قائمة الفصول المحلية
        chapters = await self.client.get_chapters_list(manga_id)
        
        # البحث عن الفصل المطلوب في القائمة
        target_chapter = None
        req_ch_num = None
        ch_slug = parts[-1]
        
        num_match = re.search(r"(\d+(?:\.\d+)?)", ch_slug)
        if num_match:
            try:
                req_ch_num = float(num_match.group(1))
            except ValueError:
                pass

        for ch in chapters:
            ch_url = ch["url"]
            if ch_url == chapter_path or ch_url.rstrip("/") == chapter_path.rstrip("/"):
                target_chapter = ch
                break
            
            # مطابقة برقم الفصل
            if req_ch_num is not None and abs(ch["chapterNumber"] - req_ch_num) < 0.01:
                target_chapter = ch
                break

        if not target_chapter:
            logger.warning(f"Could not match chapter by path {chapter_path}. Trying name match...")
            for ch in chapters:
                ch_name = ch["name"].lower()
                if ch_slug.replace("-", " ") in ch_name or (req_ch_num is not None and str(int(req_ch_num)) in ch_name):
                    target_chapter = ch
                    break

        if not target_chapter:
            if retry_on_failure:
                logger.info(f"Chapter slug {ch_slug} not found. Triggering maintenance and retrying...")
                await self.client.maintenance(force_update=True)
                return await self.get_images(url, retry_on_failure=False)
            logger.error(f"Could not find matching chapter in Suwayomi for chapter slug: {ch_slug}")
            return []

        # جلب الصفحات
        logger.info(f"Fetching pages for chapter ID {target_chapter['id']}...")
        pages = await self.client.fetch_chapter_pages(target_chapter["id"])
        
        # تطبيع الروابط النسبية وتحويلها إلى مطلقة تشير لخادم Suwayomi
        normalized_pages = []
        for p in pages:
            if p.startswith("/api/"):
                normalized_pages.append(self.client.base_url + p)
            else:
                normalized_pages.append(p)
                
        return normalized_pages

    async def get_all_chapters(self, series_url: str, retry_on_failure: bool = True) -> dict[float, str]:
        if os.getenv("HF_WORKER_RUNTIME") != "1" and os.getenv("HF_WORKER_URL"):
            logger.info(f"Delegating Suwayomi get_all_chapters to HF Worker: {series_url}")
            return await self._worker_extract_chapters(series_url)

        domain = self._normalize_domain(series_url)
        source_id, _ = await self.get_source_by_domain(domain, retry_on_failure=retry_on_failure)
        if not source_id:
            return {}

        parsed = urlparse(series_url)
        path = parsed.path
        parts = [p for p in path.split("/") if p]
        
        manga_id = await self.client.import_manga(source_id, path)
        if not manga_id and parts:
            manga_title = parts[-1].replace("-", " ").title()
            search_results = await self.client.search_manga_on_source(source_id, manga_title)
            if search_results:
                manga_id = search_results[0]["id"]

        if not manga_id:
            if retry_on_failure:
                logger.info(f"Manga import failed for {path}. Triggering maintenance and retrying...")
                await self.client.maintenance(force_update=True)
                return await self.get_all_chapters(series_url, retry_on_failure=False)
            return {}

        await self.client.fetch_chapters(manga_id)
        chapters = await self.client.get_chapters_list(manga_id)
        
        result = {}
        for ch in chapters:
            abs_url = urljoin(series_url, ch["url"])
            result[ch["chapterNumber"]] = abs_url
            
        return result
