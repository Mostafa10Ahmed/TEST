import os
import zipfile
import requests
import cloudscraper
import aiohttp
import uuid
import shutil
import asyncio
import json
import time
import concurrent.futures
from functools import partial
try:
    from bot_config import Config
except ImportError:
    from config import Config
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from providers.manager import ProviderManager
from smart_stitch import smart_stitch_from_zip
try:
    from image_filter import default_image_filter
except ImportError:
    default_image_filter = None

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# Thread pool for parallel uploads (GDrive etc.)
_UPLOAD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="upload")


class MangaDownloader:
    def __init__(self):
        self.provider_manager = ProviderManager()
        self.scraper          = self.provider_manager.generic.scraper
        self.temp_dir         = "temp_downloads"
        self._session         = None
        os.makedirs(self.temp_dir, exist_ok=True)
        # Create a standard requests.Session for direct/fallback downloads with pooling
        self._requests_session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self._requests_session.mount('https://', adapter)
        self._requests_session.mount('http://', adapter)

    async def get_session(self):
        """إعادة استخدام session واحد مع connection pooling"""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=20,  # الحد الأقصى للاتصالات المتوازية
                limit_per_host=10,  # الحد الأقصى لكل host
                force_close=False,
                enable_cleanup_closed=True
            )
            timeout = aiohttp.ClientTimeout(total=300)
            self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self._session

    async def close_session(self):
        """إغلاق session عند إيقاف البوت"""
        if self._session and not self._session.closed:
            await self._session.close()
        if hasattr(self, "_requests_session"):
            self._requests_session.close()

    @staticmethod
    def detect_image_extension(raw_bytes: bytes, img_url: str = "") -> str:
        """يكتشف الامتداد الصحيح للصورة من البايتات (Magic Bytes) لتفادي أسماء الامتدادات الغريبة مثل .stor أو .h"""
        if not raw_bytes:
            return 'jpg'
        if raw_bytes.startswith(b'\xff\xd8'):
            return 'jpg'
        elif raw_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
            return 'png'
        elif raw_bytes.startswith(b'RIFF') and len(raw_bytes) >= 12 and raw_bytes[8:12] == b'WEBP':
            return 'webp'
        elif raw_bytes.startswith(b'GIF87a') or raw_bytes.startswith(b'GIF89a'):
            return 'gif'
        elif b'ftypavif' in raw_bytes[:32] or b'ftypheic' in raw_bytes[:32]:
            return 'avif'

        # Fallback to URL extension
        clean_url = img_url.split('?')[0].split('#')[0]
        ext = clean_url.split('.')[-1].lower()[:4]
        if ext in ('jpg', 'jpeg', 'png', 'webp', 'gif', 'avif'):
            return 'jpg' if ext == 'jpeg' else ext
        return 'jpg'

    # ── شريط التقدم ───────────────────────────────────────────────────────
    @staticmethod
    def create_progress_bar(current, total, length=15, style="modern"):
        styles = {
            "modern":  ("▰", "▱", "", ""),
            "dots":    ("●", "○", "", ""),
            "square":  ("■", "□", "", ""),
            "classic": ("#", "-", "[", "]"),
        }
        fill, empty, pre, suf = styles.get(style, styles["modern"])
        if total <= 0:
            return f"{pre}{empty * length}{suf} 0%"
        pct    = max(0.0, min(1.0, float(current) / float(total)))
        filled = int(round(pct * length))
        return f"{pre}{fill * filled}{empty * (length - filled)}{suf} {int(round(pct * 100))}%"

    # ── تحميل فصل ─────────────────────────────────────────────────────────
    async def download_chapter(self, url: str, chapter_title: str, progress_callback=None, img_urls: list = None, **kwargs):
        loop     = asyncio.get_event_loop()
        # Allow callers to pass pre-fetched image URLs (e.g. from ComixProvider)
        # to avoid launching a second Playwright session unnecessarily.
        if img_urls is None:
            img_urls = await self.provider_manager.get_images(url)
        if not img_urls:
            return None

        job_id  = str(uuid.uuid4())[:8]
        job_dir = os.path.join(self.temp_dir, job_id)
        os.makedirs(job_dir)
        downloaded_files = []
        completed        = 0

        failed_indices: list[int] = []

        def download_single(idx, img_url):
            nonlocal failed_indices
            from providers.base_provider import get_cookies_for_url
            
            cookies = get_cookies_for_url(url)
            
            headers = {
                "Referer":    url,
                "Origin":     "https://" + url.split("/")[2] if "://" in url else url,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept":     "image/webp,image/apng,image/*,*/*;q=0.8",
            }
            
            # Direct provider specific headers for sensitive CDNs
            if "acimg.cn" in img_url or "ac.qq.com" in url:
                headers["Referer"] = "https://ac.qq.com/"
            elif "kkmh.com" in img_url or "kuaikanmanhua.com" in url:
                headers["Referer"] = "https://www.kuaikanmanhua.com/"
            elif "cdnlibs.org" in img_url or "hentaicdn.org" in img_url or "mangalib" in url:
                headers["Referer"] = "https://mangalib.me/"
            elif "mangabuff.ru" in img_url or "mangabuff.ru" in url:
                headers["Referer"] = "https://mangabuff.ru/"
            
            # Apply custom User-Agent if present in cookies
            if cookies and "__custom_user_agent" in cookies:
                headers["User-Agent"] = cookies.pop("__custom_user_agent")
                
            # Apply Bearer token if access_token is present
            if cookies and "access_token" in cookies:
                headers["Authorization"] = f"Bearer {cookies['access_token']}"
                
            # Filter cookies to only actual cookie keys
            cookie_dict = {}
            if cookies:
                for k, v in cookies.items():
                    if k not in ("access_token", "refresh_token", "__custom_user_agent"):
                        cookie_dict[k] = v
                        
            if img_url.startswith("data:image/"):
                try:
                    import base64
                    header, b64data = img_url.split(",", 1)
                    raw = base64.b64decode(b64data)
                    if default_image_filter:
                        valid, reason = default_image_filter.is_valid_manga_page(raw)
                        if not valid:
                            print(f"Skipping image {idx}: {reason}")
                            return None
                    ext = self.detect_image_extension(raw, img_url)
                    fp = os.path.join(job_dir, f"{idx:03d}.{ext}")
                    with open(fp, 'wb') as f:
                        f.write(raw)
                    return fp
                except Exception as e:
                    print(f"Error decoding dataURL image {idx}: {e}")
                    return None

            for attempt in range(3):
                try:
                    # Use standard requests directly for CDNs that reject cloudscraper TLS fingerprints
                    if any(k in img_url for k in ["asura", "cdn.asurascans.com", "acimg.cn", "kkmh.com", "cdnlibs.org", "hentaicdn.org", "mangabuff.ru", "meo.comick.pictures"]):
                        r = self._requests_session.get(img_url, stream=True, timeout=(10, 90), headers=headers, cookies=cookie_dict)
                    else:
                        r = self.scraper.get(img_url, stream=True, timeout=(10, 90), headers=headers, cookies=cookie_dict)

                    if r.status_code == 200:
                        raw  = r.content
                        if default_image_filter:
                            valid, reason = default_image_filter.is_valid_manga_page(raw)
                            if not valid:
                                print(f"Skipping image {idx} ({img_url[:60]}): {reason}")
                                return None
                        ext = self.detect_image_extension(raw, img_url)
                        fp = os.path.join(job_dir, f"{idx:03d}.{ext}")
                        with open(fp, 'wb') as f:
                            f.write(raw)
                        return fp
                    elif r.status_code == 429:
                        time.sleep(2 ** attempt)  # rate limit backoff
                    elif r.status_code in (403, 401):
                        print(f"Image {idx} blocked ({r.status_code}): {img_url[:80]}")
                        break  # No point retrying auth errors
                except Exception as e:
                    # Fallback to standard requests (non-cloudscraper) in case cloudscraper's customized SSL adapters fail
                    try:
                        print(f"cloudscraper failed for image {idx} (attempt {attempt}): {e}. Trying standard requests session...")
                        r = self._requests_session.get(img_url, stream=True, timeout=(10, 90), headers=headers, cookies=cookie_dict)
                        if r.status_code == 200:
                            raw  = r.content
                            if default_image_filter:
                                valid, reason = default_image_filter.is_valid_manga_page(raw)
                                if not valid:
                                    print(f"Skipping image {idx} ({img_url[:60]}): {reason}")
                                    return None
                            ext = self.detect_image_extension(raw, img_url)
                            fp = os.path.join(job_dir, f"{idx:03d}.{ext}")
                            with open(fp, 'wb') as f:
                                f.write(raw)
                            return fp
                    except Exception as fallback_e:
                        print(f"Standard requests session fallback also failed for image {idx}: {fallback_e}")
                    
                    if attempt == 2:
                        print(f"Image {idx} failed after 3 attempts: {e}")
                    else:
                        time.sleep(2)
            failed_indices.append(idx)
            return None

        # Reduce concurrency to 5 to avoid overwhelming CDNs with connection limits
        sem = asyncio.Semaphore(5)

        async def dl_limited(idx, u):
            async with sem:
                return await loop.run_in_executor(None, download_single, idx, u)

        tasks = [dl_limited(i, u) for i, u in enumerate(img_urls)]
        for task in asyncio.as_completed(tasks):
            fp = await task
            if fp:
                downloaded_files.append(fp)
            completed += 1
            if progress_callback and (completed % 2 == 0 or completed == len(img_urls)):
                await progress_callback(completed, len(img_urls), "📥 تحميل الصور")

        if not downloaded_files:
            shutil.rmtree(job_dir)
            return None

        # تنبيه إذا فشلت صور
        if failed_indices:
            msg = f"⚠️ [Download] {len(failed_indices)} صور فشلت: {failed_indices[:10]}"
            if len(failed_indices) > 10:
                msg += f" ...و {len(failed_indices) - 10} أخرى"
            print(msg)
            if progress_callback:
                await progress_callback(
                    completed, len(img_urls),
                    f"⚠️ فشلت {len(failed_indices)} صور (انتبه: قد يكون الفصل ناقص)"
                )

        downloaded_files.sort()
        zip_name = f"{chapter_title.replace(' ', '_')}_{job_id}.zip"
        zip_path = os.path.join(self.temp_dir, zip_name)
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for f in downloaded_files:
                zf.write(f, os.path.basename(f))
        shutil.rmtree(job_dir)
        return zip_path

    # ── SmartStitch ────────────────────────────────────────────────────────
    async def download_and_stitch(
        self, url: str, chapter_title: str,
        target_height: int = 14500, target_width: int = 800,
        sensitivity: int = 90, progress_callback=None, 
        upload_dest: str = "Auto", folder_id: str = None,
        img_urls: list = None, **_
    ) -> dict | None:
        """
        يُرجع قاموساً يحتوي على الرابط والنوع (drive_folder, gofile, catbox, local_zip)
        """
        loop    = asyncio.get_event_loop()
        raw_zip = await self.download_chapter(url, chapter_title, progress_callback=progress_callback, img_urls=img_urls)
        if not raw_zip:
            return None
        if progress_callback:
            await progress_callback(0, 1, "🪡 دمج الصور (SmartStitch)...")

        stitch_dir = os.path.join(self.temp_dir, f"stitched_{uuid.uuid4().hex[:8]}")
        safe_title = chapter_title.replace(" ", "_")

        def run_stitch():
            return smart_stitch_from_zip(
                zip_path=raw_zip, output_dir=stitch_dir, chapter_name=safe_title,
                target_height=target_height, target_width=target_width,
                sensitivity=sensitivity, output_format="jpg", output_quality=95,
            )

        stitched_files = await loop.run_in_executor(None, run_stitch)
        self.cleanup(raw_zip)
        
        if not stitched_files:
            shutil.rmtree(stitch_dir, ignore_errors=True)
            return None

        # ── الترتيب للرفع ──────────────────────────────────────────────────
        
        # إنشاء ملف ZIP محلي
        final_zip = os.path.join(self.temp_dir, f"{safe_title}_stitched_{uuid.uuid4().hex[:8]}.zip")
        with zipfile.ZipFile(final_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in stitched_files:
                zf.write(f, os.path.basename(f))

        if upload_dest == "Local":
            shutil.rmtree(stitch_dir, ignore_errors=True)
            return {"link": final_zip, "type": "local_zip"}

        # 1. محاولة Google Drive أولاً (الأولوية)
        if upload_dest in ("Auto", "Drive") and Config.GOOGLE_DRIVE_FOLDER_ID:
            try:
                if progress_callback:
                    await progress_callback(90, 100, "📂 رفع إلى Google Drive...")
                drive_link = await self.upload_stitched_to_gdrive(stitched_files, safe_title, progress_callback, parent_folder_id=folder_id)
                if drive_link:
                    self.cleanup(final_zip)
                    shutil.rmtree(stitch_dir, ignore_errors=True)
                    return {"link": drive_link, "type": "drive_folder"}
            except Exception as e:
                print(f"Drive upload failed, falling back: {e}")

        # 2. محاولة Gofile
        if upload_dest in ("Auto", "Gofile"):
            try:
                if progress_callback:
                    p_msg = "☁️ رفع إلى Gofile..." if upload_dest == "Gofile" else "☁️ Drive فشل، محاولة Gofile..."
                    await progress_callback(95, 100, p_msg)
                link = await self.upload_to_gofile(final_zip, progress_callback=progress_callback, folder_id=folder_id)
                if link:
                    shutil.rmtree(stitch_dir, ignore_errors=True)
                    self.cleanup(final_zip)
                    return {"link": link, "type": "gofile"}
            except Exception:
                pass

        # 3. محاولة Catbox
        if upload_dest in ("Auto", "Catbox"):
            try:
                if progress_callback:
                    p_msg = "📦 محاولة Catbox..." if upload_dest == "Catbox" else "📦 فشل السابق، محاولة Catbox..."
                    await progress_callback(98, 100, p_msg)
                link = await self.upload_to_catbox(final_zip)
                if link:
                    shutil.rmtree(stitch_dir, ignore_errors=True)
                    self.cleanup(final_zip)
                    return {"link": link, "type": "catbox"}
            except Exception:
                pass

        # تنظيف في حال الفشل التام
        shutil.rmtree(stitch_dir, ignore_errors=True)
        return {"link": final_zip, "type": "local_zip"}

    # ── رفع Gofile ────────────────────────────────────────────────────────
    async def create_gofile_folder(self, folder_name: str):
        """ينشئ مجلداً في Gofile داخل rootFolder للحساب."""
        if not Config.GOFILE_TOKEN:
            return None
        try:
            async with aiohttp.ClientSession() as s:
                hdrs = {"Authorization": f"Bearer {Config.GOFILE_TOKEN}"}

                # 1. الحصول على accountId
                account_id = None
                async with s.get("https://api.gofile.io/accounts/getid", headers=hdrs) as r:
                    if r.status == 200:
                        acc_id_data = await r.json()
                        account_id = acc_id_data.get("data", {}).get("id")

                if not account_id:
                    print("Gofile: فشل الحصول على accountId")
                    return None

                # 2. الحصول على rootFolder id
                root_id = None
                async with s.get(f"https://api.gofile.io/accounts/{account_id}", headers=hdrs) as r:
                    if r.status == 200:
                        acc_data = await r.json()
                        root_id = acc_data.get("data", {}).get("rootFolder")

                if not root_id:
                    print("Gofile: فشل الحصول على rootFolder")
                    return None

                # 3. إنشاء المجلد داخل rootFolder
                data = {"folderName": folder_name, "parentFolderId": root_id}
                async with s.post("https://api.gofile.io/contents/createFolder", json=data, headers=hdrs) as r:
                    if r.status == 200:
                        res = await r.json()
                        if res.get("status") == "ok":
                            return res.get("data", {})
                    else:
                        print(f"Gofile Folder Creation failed: HTTP {r.status}")
        except Exception as e:
            print(f"Gofile Folder Creation Error: {e}")
        return None

    async def upload_to_gofile(
        self,
        file_path: str,
        progress_callback=None,
        folder_id: str = None,
        remote_filename: str = None,
    ):
        async def _upload():
            try:
                if progress_callback:
                    await progress_callback(0, 100, "☁️ جلب سيرفر Gofile...")
                
                server = "store1"
                async with aiohttp.ClientSession() as s:
                    async with s.get("https://api.gofile.io/getServer") as r:
                        if r.status == 200:
                            srv_data = await r.json()
                            server = srv_data.get("data", {}).get("server") or "store1"

                if progress_callback:
                    await progress_callback(10, 100, f"☁️ رفع إلى Gofile ({server})")

                filename = remote_filename or os.path.basename(file_path)
                hdrs = {}
                if Config.GOFILE_TOKEN:
                    hdrs["Authorization"] = f"Bearer {Config.GOFILE_TOKEN}"

                with open(file_path, "rb") as fp:
                    data = aiohttp.FormData()
                    data.add_field("file", fp, filename=filename)
                    if folder_id:
                        data.add_field("folderId", folder_id)

                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800)) as s:
                        async with s.post(f"https://{server}.gofile.io/uploadFile", data=data, headers=hdrs) as r:
                            txt = await r.text()
                            if r.status == 200:
                                pl = json.loads(txt)
                                if pl.get("status") in ("ok", True):
                                    d = pl.get("data", {})
                                    link = d.get("downloadPage") or d.get("pageLink") or d.get("directLink") or d.get("link")
                                    if link:
                                        return link
                            return None
            except Exception as e:
                print(f"Gofile error: {e}")
                return None

        for attempt in range(3):
            link = await _upload()
            if link: return link
            await asyncio.sleep(5)
        return None

    # ── رفع Catbox (بديل مجاني بلا حساب) ────────────────────────────────
    async def upload_to_catbox(self, file_path: str, progress_callback=None):
        """رفع إلى catbox.moe — مجاني 200MB max."""
        try:
            if progress_callback:
                await progress_callback(0, 100, "☁️ رفع إلى Catbox")
            filename = os.path.basename(file_path)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
                with open(file_path, "rb") as f:
                    data = aiohttp.FormData()
                    data.add_field("reqtype", "fileupload")
                    data.add_field("fileToUpload", f, filename=filename, content_type="application/zip")
                    async with s.post("https://catbox.moe/user/api.php", data=data) as r:
                        text = await r.text()
                        if r.status == 200 and text.startswith("https://"):
                            if progress_callback:
                                await progress_callback(100, 100, "☁️ رفع إلى Catbox")
                            return text.strip()
                        print(f"Catbox error: {r.status} {text[:200]}")
                        return None
        except Exception as e:
            print(f"Catbox error: {e}")
            return None

            # ── Google Drive Helper ──────────────────────────────────────────────
    def _get_drive_service(self):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from pathlib import Path

        creds = None
        
        # 1. Try to load from GOOGLE_DRIVE_TOKEN_JSON environment variable
        token_env = os.getenv("GOOGLE_DRIVE_TOKEN_JSON") or getattr(Config, "GOOGLE_DRIVE_TOKEN_JSON", None)
        if token_env:
            try:
                token_env = token_env.strip()
                if token_env.startswith("'") and token_env.endswith("'"):
                    token_env = token_env[1:-1]
                info = json.loads(token_env)
                creds = Credentials.from_authorized_user_info(info, DRIVE_SCOPES)
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
            except Exception as e:
                try:
                    _safe_print(f"Failed to load from GOOGLE_DRIVE_TOKEN_JSON env: {e}")
                except NameError:
                    print(f"Failed to load from GOOGLE_DRIVE_TOKEN_JSON env: {e}")

        # 2. Try to load from local token.json file
        if not creds:
            token_paths = [
                Path("token.json"),
                Path(__file__).parent / "token.json",
                Path(__file__).parent.parent / "token.json"
            ]
            for p in token_paths:
                if p.exists():
                    try:
                        creds = Credentials.from_authorized_user_file(str(p), DRIVE_SCOPES)
                        if creds and creds.expired and creds.refresh_token:
                            creds.refresh(Request())
                            try:
                                with open(p, "w") as tf:
                                    tf.write(creds.to_json())
                            except Exception:
                                pass
                        break
                    except Exception as e:
                        try:
                            _safe_print(f"Failed to load token.json from {p}: {e}")
                        except NameError:
                            print(f"Failed to load token.json from {p}: {e}")

        if creds:
            return build("drive", "v3", credentials=creds)

        # 3. Fallback to Service Account JSON
        raw_json = Config.GOOGLE_SERVICE_ACCOUNT_JSON
        if not raw_json:
            raise Exception("No valid Google Drive credentials found (GOOGLE_DRIVE_TOKEN_JSON/token.json/GOOGLE_SERVICE_ACCOUNT_JSON not set)")
        
        raw_json = raw_json.strip()
        if raw_json.startswith("'") and raw_json.endswith("'"):
            raw_json = raw_json[1:-1]
        
        info = json.loads(raw_json)
        if "private_key" in info:
            info["private_key"] = info["private_key"].replace("\\n", "\n")
        
        creds = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
        return build('drive', 'v3', credentials=creds)

    async def create_gdrive_folder(self, folder_name: str, parent_id: str = None):
        loop = asyncio.get_event_loop()
        def _create():
            try:
                service = self._get_drive_service()
                p_id = parent_id or Config.GOOGLE_DRIVE_FOLDER_ID
                
                # Get driveId if parent is in a shared drive
                p_meta = service.files().get(fileId=p_id, fields="driveId", supportsAllDrives=True).execute()
                drive_id = p_meta.get("driveId")

                file_meta = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [p_id]}
                if drive_id: file_meta['driveId'] = drive_id
                
                f = service.files().create(body=file_meta, fields='id,webViewLink', supportsAllDrives=True).execute()
                # Make public
                service.permissions().create(fileId=f['id'], body={'type': 'anyone', 'role': 'reader'}, supportsAllDrives=True).execute()
                return f
            except Exception as e:
                try:
                    _safe_print(f"Drive Create Folder Error: {e}")
                except NameError:
                    print(f"Drive Create Folder Error: {e}")
                return None
        return await loop.run_in_executor(None, _create)

    async def upload_to_gdrive(self, file_path: str, filename: str, progress_callback=None, parent_folder_id: str = None):
        loop = asyncio.get_event_loop()

        def _upload():
            try:
                p_id = parent_folder_id or Config.GOOGLE_DRIVE_FOLDER_ID
                if not p_id: return None

                service = self._get_drive_service()

                p_meta = service.files().get(fileId=p_id, fields="id,name,driveId", supportsAllDrives=True).execute()
                drive_id = p_meta.get("driveId")

                file_meta = {'name': filename, 'parents': [p_id]}
                kwargs    = dict(body=file_meta, media_body=MediaFileUpload(file_path, resumable=True, chunksize=5*1024*1024),
                                 fields='id,webViewLink,webContentLink', supportsAllDrives=True)
                if drive_id:
                    kwargs['driveId'] = drive_id

                req      = service.files().create(**kwargs)
                response = None
                while response is None:
                    status, response = req.next_chunk()
                    if status and progress_callback:
                        asyncio.run_coroutine_threadsafe(
                            progress_callback(int(status.progress() * 100), 100, "☁️ Uploading to Google Drive..."),
                            loop
                        )

                file_id = response.get('id')
                service.permissions().create(
                    fileId=file_id,
                    body={'type': 'anyone', 'role': 'reader'},
                    supportsAllDrives=True
                ).execute()
                if progress_callback:
                    asyncio.run_coroutine_threadsafe(
                        progress_callback(100, 100, "☁️ Upload to Google Drive complete."), loop
                    )
                return (response.get('webViewLink') or
                        response.get('webContentLink') or
                        f"https://drive.google.com/file/d/{file_id}/view?usp=sharing")

            except HttpError as e:
                msg = str(e)
                if "storageQuotaExceeded" in msg:
                    sa_email = "SA"
                    try:
                        sa_email = json.loads(Config.GOOGLE_SERVICE_ACCOUNT_JSON).get("client_email", "SA")
                    except Exception:
                        pass
                    try:
                        _safe_print(f"❌ Drive: Storage quota exceeded — share a Shared Drive with: {sa_email}")
                    except NameError:
                        print(f"❌ Drive: Storage quota exceeded — share a Shared Drive with: {sa_email}")
                elif "403" in msg or "forbidden" in msg.lower():
                    try:
                        _safe_print(f"❌ Drive: Insufficient permissions — {e}")
                    except NameError:
                        print(f"❌ Drive: Insufficient permissions — {e}")
                else:
                    try:
                        _safe_print(f"❌ Drive HTTP Error: {e}")
                    except NameError:
                        print(f"❌ Drive HTTP Error: {e}")
                return None
            except Exception as e:
                try:
                    _safe_print(f"❌ Drive error: {e}")
                except NameError:
                    print(f"❌ Drive error: {e}")
                return None

        for attempt in range(2):
            link = await loop.run_in_executor(None, _upload)
            if link:
                return link
            await asyncio.sleep(3)
        return None

    # ── رفع مجلد صور مدمجة إلى Google Drive (متوازي) ─────────────────────
    async def upload_stitched_to_gdrive(self, file_paths: list, folder_name: str, progress_callback=None, parent_folder_id: str = None):
        loop = asyncio.get_event_loop()

        def _build_service():
            return self._get_drive_service()

        def _upload_one(service, folder_id, fp, idx, total):
            fname = os.path.basename(fp)
            m_meta = {'name': fname, 'parents': [folder_id]}
            media = MediaFileUpload(fp, mimetype='image/jpeg')
            service.files().create(
                body=m_meta,
                media_body=media,
                supportsAllDrives=True
            ).execute()
            if progress_callback:
                asyncio.run_coroutine_threadsafe(
                    progress_callback(idx + 1, total, f"📤 Uploading part {idx+1}/{total} to Drive"),
                    loop
                )

        def _upload_all():
            try:
                service = _build_service()
                if not service: return None

                # 1. إنشاء المجلد
                p_id = parent_folder_id or Config.GOOGLE_DRIVE_FOLDER_ID
                file_meta = {
                    'name': folder_name,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [p_id]
                }
                
                parent_meta = service.files().get(
                    fileId=p_id, fields="driveId", supportsAllDrives=True
                ).execute()
                drive_id = parent_meta.get("driveId")
                if drive_id:
                    file_meta['driveId'] = drive_id

                folder = service.files().create(
                    body=file_meta, fields='id,webViewLink', supportsAllDrives=True
                ).execute()
                folder_id = folder.get('id')

                # 2. جعل المجلد عاماً
                service.permissions().create(
                    fileId=folder_id,
                    body={'type': 'anyone', 'role': 'reader'},
                    supportsAllDrives=True
                ).execute()

                # 3. رفع الصور بالتوازي (4 فايلات مع بعض)
                from concurrent.futures import ThreadPoolExecutor
                total = len(file_paths)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futs = []
                    for i, fp in enumerate(file_paths):
                        svc = _build_service()  # service لكل thread
                        futs.append(pool.submit(_upload_one, svc, folder_id, fp, i, total))
                    for f in futs:
                        f.result()  # raise إن وجد

                return folder.get('webViewLink') or f"https://drive.google.com/drive/folders/{folder_id}"

            except Exception as e:
                try:
                    _safe_print(f"❌ upload_stitched_to_gdrive error: {e}")
                except NameError:
                    print(f"❌ upload_stitched_to_gdrive error: {e}")
                return None

        return await loop.run_in_executor(None, _upload_all)

# ── تنظيف ─────────────────────────────────────────────────────────────
    def cleanup(self, file_path: str):
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Cleanup error: {e}")
