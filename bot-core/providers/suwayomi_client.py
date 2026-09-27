import aiohttp
import json
import logging
import asyncio
import os
import time
import subprocess
import sys
from bot_config import Config

logger = logging.getLogger("SuwayomiClient")

class SuwayomiClient:
    def __init__(self, base_url: str = None):
        self.base_url = (base_url or Config.SUWAYOMI_URL).rstrip("/")
        self.graphql_url = f"{self.base_url}/api/graphql"
        self.rest_url = f"{self.base_url}/api/v1"

    async def execute_graphql(self, query: str, variables: dict = None) -> dict:
        url = self.graphql_url
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=45) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "errors" in data:
                            logger.error(f"GraphQL Errors: {data['errors']}")
                            return {}
                        return data.get("data", {})
                    else:
                        logger.error(f"GraphQL HTTP Error {resp.status}: {await resp.text()}")
        except Exception as e:
            logger.error(f"GraphQL Connection Error: {e}")
        return {}

    async def execute_rest_get(self, endpoint: str, params: dict = None) -> dict:
        url = f"{self.rest_url}/{endpoint.lstrip('/')}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=45) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
                    else:
                        logger.error(f"REST GET Error {resp.status}: {await resp.text()}")
        except Exception as e:
            logger.error(f"REST GET Connection Error: {e}")
        return {}

    async def execute_rest_post(self, endpoint: str, json_data: dict = None, params: dict = None) -> dict:
        url = f"{self.rest_url}/{endpoint.lstrip('/')}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=json_data, params=params, timeout=45) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
                    else:
                        logger.error(f"REST POST Error {resp.status}: {await resp.text()}")
        except Exception as e:
            logger.error(f"REST POST Connection Error: {e}")
        return {}

    async def check_health(self) -> bool:
        """تحقق من صحة الخادم عبر استعلام GraphQL بسيط."""
        query = """
        query {
          aboutServer {
            version
            name
          }
        }
        """
        res = await self.execute_graphql(query)
        return bool(res and "aboutServer" in res)

    async def add_keiyoushi_repo(self) -> bool:
        """إضافة مستودع Keiyoushi لإضافات Tachiyomi/Mihon."""
        query = """
        query {
          settings {
            extensionRepos
          }
        }
        """
        res = await self.execute_graphql(query)
        if not res:
            return False
        repos = res.get("settings", {}).get("extensionRepos", [])
        keiyoushi_url = "https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.min.json"
        if keiyoushi_url in repos:
            return True

        # إضافة المستودع
        new_repos = list(repos) + [keiyoushi_url]
        mutation = """
        mutation SetSettings($input: SetSettingsInput!) {
          setSettings(input: $input) {
            settings {
              extensionRepos
            }
          }
        }
        """
        vars = {
            "input": {
                "settings": {
                    "extensionRepos": new_repos
                }
            }
        }
        m_res = await self.execute_graphql(mutation, vars)
        return bool(m_res)

    async def fetch_extensions_list(self) -> bool:
        """جلب وتحديث قائمة الإضافات المتوفرة في المستودعات."""
        mutation = """
        mutation {
          fetchExtensions(input: {}) {
            extensions {
              pkgName
            }
          }
        }
        """
        res = await self.execute_graphql(mutation)
        return bool(res)

    async def get_extensions(self) -> list:
        """الحصول على قائمة بكل الإضافات والمصادر التابعة لها."""
        query = """
        query {
          extensions {
            nodes {
              pkgName
              name
              lang
              isInstalled
              hasUpdate
              source {
                nodes {
                  id
                  name
                }
              }
            }
          }
        }
        """
        res = await self.execute_graphql(query)
        return res.get("extensions", {}).get("nodes", [])

    async def install_or_update_extension(self, pkg_name: str, install: bool = True, update: bool = False) -> bool:
        """تثبيت أو تحديث إضافة معينة."""
        mutation = """
        mutation UpdateExt($pkg: String!, $patch: UpdateExtensionPatchInput!) {
          updateExtension(input: { id: $pkg, patch: $patch }) {
            extension {
              pkgName
              isInstalled
            }
          }
        }
        """
        vars = {
            "pkg": pkg_name,
            "patch": {
                "install": install,
                "update": update
            }
        }
        res = await self.execute_graphql(mutation, vars)
        return bool(res)

    async def import_manga(self, source_id: str, relative_url: str) -> int:
        """استيراد المانجا من المصدر إلى قاعدة البيانات للحصول على معرف محلي."""
        url_param = "/" + relative_url.lstrip("/")
        endpoint = f"source/{source_id}/manga"
        
        # 1. محاولة استخدام REST API
        res = await self.execute_rest_get(endpoint, params={"url": url_param})
        if res and "id" in res:
            return res["id"]

        # 2. محاولة استخدام GraphQL كبديل احتياطي
        # سنحاول البحث عن المانجا بالرابط النسبي أو الكامل
        logger.info(f"REST import failed for {url_param}. Trying GraphQL search fallback...")
        mangas = await self.search_manga_on_source(source_id, url_param)
        if mangas:
            return mangas[0]["id"]
            
        # محاولة البحث بالرابط الكامل
        query = """
        query {
          source(id: "%s") {
            name
          }
        }
        """ % source_id
        source_res = await self.execute_graphql(query)
        source_name = source_res.get("source", {}).get("name")
        logger.info(f"GraphQL search by relative URL failed. Searching by name or exact query...")
        return None

    async def search_manga_on_source(self, source_id: str, query_str: str) -> list:
        """البحث عن مانجا داخل مصدر معين."""
        mutation = """
        mutation FetchSourceManga($input: FetchSourceMangaInput!) {
          fetchSourceManga(input: $input) {
            mangas {
              id
              title
              url
            }
          }
        }
        """
        vars = {
            "input": {
                "source": source_id,
                "query": query_str,
                "page": 1,
                "type": "SEARCH"
            }
        }
        res = await self.execute_graphql(mutation, vars)
        return res.get("fetchSourceManga", {}).get("mangas", [])

    async def fetch_chapters(self, manga_id: int) -> bool:
        """جلب الفصول الجديدة للمانجا وتحديثها من المصدر."""
        mutation = """
        mutation FetchChapters($id: Int!) {
          fetchChapters(input: { id: $id }) {
            manga {
              id
            }
          }
        }
        """
        res = await self.execute_graphql(mutation, {"id": manga_id})
        return bool(res)

    async def get_chapters_list(self, manga_id: int) -> list:
        """جلب قائمة الفصول للمانجا المخزنة محلياً."""
        query = """
        query GetChapters($mangaId: Int!) {
          manga(id: $mangaId) {
            chapters {
              nodes {
                id
                url
                name
                chapterNumber
              }
            }
          }
        }
        """
        res = await self.execute_graphql(query, {"mangaId": manga_id})
        return res.get("manga", {}).get("chapters", {}).get("nodes", [])

    async def fetch_chapter_pages(self, chapter_id: int) -> list:
        """جلب روابط صور الصفحات لفصل معين."""
        mutation = """
        mutation FetchPages($chapterId: Int!) {
          fetchChapterPages(input: { chapterId: $chapterId }) {
            pages
          }
        }
        """
        res = await self.execute_graphql(mutation, {"chapterId": chapter_id})
        return res.get("fetchChapterPages", {}).get("pages", [])

    async def clear_cache(self) -> bool:
        """تفريغ الصور المصغرة والصفحات المؤقتة لتوفير مساحة القرص."""
        mutation = """
        mutation {
          clearCachedImages(input: { cachedPages: true, cachedThumbnails: true }) {
            clientMutationId
          }
        }
        """
        res = await self.execute_graphql(mutation)
        return bool(res)

    async def bootstrap(self) -> bool:
        """
        يقوم بتهيئة خادم Suwayomi وتثبيت المستودعات المطلوبة وتحديث الإضافات إذا لزم الأمر.
        """
        logger.info("Starting Suwayomi-Server bootstrap...")

        # 0. تشغيل الخادم تلقائياً في الخلفية إذا لم يكن قيد التشغيل
        if not await self.check_health():
            logger.info("Suwayomi server is not running. Attempting to spawn it in the background...")
            base_dir = os.getcwd()
            temp_dir = os.path.join(base_dir, "temp_downloads")
            os.makedirs(temp_dir, exist_ok=True)

            jar_path = os.path.join(temp_dir, "Suwayomi-Server.jar")
            
            # Find JRE executable
            jre_exe = "java"
            jre_dir = os.path.join(temp_dir, "jre21")
            possible_jre = os.path.join(jre_dir, "bin", "java.exe" if sys.platform == "win32" else "java")
            if os.path.exists(possible_jre):
                jre_exe = possible_jre
            else:
                # Check jdk-21* folders
                for item in os.listdir(temp_dir):
                    if item.startswith("jdk-21") or item.startswith("jdk21"):
                        candidate = os.path.join(temp_dir, item, "bin", "java.exe" if sys.platform == "win32" else "java")
                        if os.path.exists(candidate):
                            jre_exe = candidate
                            break

            # Auto download Suwayomi JAR if missing
            if not os.path.exists(jar_path):
                logger.info("Suwayomi-Server.jar missing. Downloading from GitHub...")
                jar_url = "https://github.com/Suwayomi/Suwayomi-Server/releases/download/v2.3.2243/Suwayomi-Server-v2.3.2243.jar"
                try:
                    subprocess.run(["curl", "-L", "-C", "-", "-o", jar_path, jar_url], check=True, timeout=600)
                except Exception as dl_err:
                    logger.error(f"Failed to download Suwayomi-Server.jar via curl: {dl_err}")

            if os.path.exists(jar_path):
                try:
                    cmd = [jre_exe, "-jar", jar_path]
                    if sys.platform == "win32":
                        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    logger.info("Suwayomi server process spawned successfully!")
                except Exception as spawn_err:
                    logger.error(f"Failed to spawn Suwayomi server process: {spawn_err}")
            else:
                logger.error(f"Suwayomi-Server.jar not found at {jar_path}. Cannot start server.")
        
        # 1. التحقق من صحة الاتصال بالخادم
        healthy = False
        for attempt in range(120):
            if await self.check_health():
                healthy = True
                logger.info("Suwayomi-Server is healthy and reachable!")
                break
            logger.info(f"Waiting for Suwayomi-Server to start... attempt {attempt+1}/120")
            await asyncio.sleep(3)
            
        if not healthy:
            logger.error("Failed to connect to Suwayomi-Server during bootstrap.")
            return False

        # 2. إضافة مستودع Keiyoushi
        logger.info("Registering Keiyoushi repository...")
        await self.add_keiyoushi_repo()

        # 3. صيانة دورية وتحديث الإضافات كل 30 يوم
        should_update_exts = True
        last_update_file = "data/suwayomi_last_update.txt"
        
        # التأكد من وجود مجلد data
        os.makedirs(os.path.dirname(last_update_file), exist_ok=True)
        
        if os.path.exists(last_update_file):
            try:
                with open(last_update_file, "r") as f:
                    last_time = float(f.read().strip())
                # 30 days in seconds = 30 * 24 * 60 * 60 = 2592000
                if time.time() - last_time < 2592000:
                    should_update_exts = False
                    logger.info("Extensions were updated less than 30 days ago. Skipping update.")
            except Exception as e:
                logger.warning(f"Failed to read last update file: {e}")

        # تفريغ الكاش دائمًا عند الإقلاع لتوفير المساحة
        await self.clear_cache()

        # [REMOVED] Pre-install all extensions loop (handled on-demand by providers)

        if should_update_exts:
            logger.info("Updating extensions (30-day cycle or first run)...")
            await self.fetch_extensions_list()
            try:
                exts = await self.get_extensions()
                for ext in exts:
                    if ext.get("isInstalled", False) and ext.get("hasUpdate", False):
                        logger.info(f"Updating installed extension: {ext['name']} ({ext['pkgName']})...")
                        await self.install_or_update_extension(ext["pkgName"], install=True, update=True)
                
                # كتابة الوقت الحالي للمزامنة القادمة
                with open(last_update_file, "w") as f:
                    f.write(str(time.time()))
            except Exception as e:
                logger.error(f"Error updating extensions during bootstrap: {e}")

        # 4. تشغيل حلقة الصيانة الخلفية كل 24 ساعة إذا لم تكن قيد التشغيل بالفعل
        if not hasattr(self, "_maintenance_loop_task") or self._maintenance_loop_task is None:
            self._maintenance_loop_task = asyncio.create_task(self.start_maintenance_loop())
            logger.info("Suwayomi background maintenance loop registered.")

        logger.info("Suwayomi-Server bootstrap completed successfully!")
        return True

    async def maintenance(self, force_update: bool = False) -> bool:
        """
        صيانة دورية لخادم Suwayomi: تفريغ الكاش، وتحديث الإضافات.
        """
        logger.info("Running Suwayomi-Server maintenance...")
        
        # 1. تفريغ كاش الصور لتوفير مساحة القرص
        logger.info("Clearing Suwayomi cached images/thumbnails...")
        try:
            await self.clear_cache()
        except Exception as e:
            logger.warning(f"Failed to clear Suwayomi cache: {e}")

        # 2. تحديث قائمة الإضافات وتثبيت التحديثات
        if force_update:
            logger.info("Force updating extensions...")
            await self.fetch_extensions_list()
            try:
                exts = await self.get_extensions()
                for ext in exts:
                    if ext.get("isInstalled", False) and ext.get("hasUpdate", False):
                        logger.info(f"Updating extension: {ext['name']} ({ext['pkgName']})...")
                        await self.install_or_update_extension(ext["pkgName"], install=True, update=True)
                
                # تحديث تاريخ المزامنة
                last_update_file = "data/suwayomi_last_update.txt"
                os.makedirs(os.path.dirname(last_update_file), exist_ok=True)
                with open(last_update_file, "w") as f:
                    f.write(str(time.time()))
            except Exception as e:
                logger.error(f"Failed to update extensions: {e}")
                    
        logger.info("Suwayomi-Server maintenance completed.")
        return True

    async def start_maintenance_loop(self):
        """
        حلقة خلفية لتشغيل الصيانة الدورية كل 24 ساعة.
        """
        logger.info("Starting Suwayomi maintenance loop...")
        while True:
            try:
                # الانتظار لمدة 24 ساعة
                await asyncio.sleep(86400)
                await self.bootstrap()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Suwayomi maintenance loop: {e}")
                await asyncio.sleep(60)
