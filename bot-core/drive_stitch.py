"""
drive_stitch.py — تحميل صور من Google Drive وتطبيق SmartStitch عليها
يدعم:
  - رابط مجلد Google Drive: https://drive.google.com/drive/folders/...
  - رابط ملف عادي (ZIP): https://drive.google.com/file/d/.../view
  - رابط مشاركة مباشر
"""

import os
import io
import re
import json
import uuid
import shutil
import asyncio
import zipfile
import tempfile
from typing import Optional, Callable

import aiohttp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError

try:
    from bot_config import Config
except ImportError:
    from config import Config
from smart_stitch import smart_stitch_to_files

DRIVE_SCOPES     = ["https://www.googleapis.com/auth/drive"]
SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def _get_drive_service():
    from pathlib import Path
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

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
            print(f"[DriveStitch] Failed to load from GOOGLE_DRIVE_TOKEN_JSON env: {e}")

    # 2. Try to load user credentials from token.json if it exists
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
                    print(f"[DriveStitch] Failed to load token.json from {p}: {e}")

    if creds:
        return build("drive", "v3", credentials=creds)

    # 3. Fallback to Service Account JSON
    if not Config.GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON غير موجود ولم يتم العثور على token.json")
    info = json.loads(Config.GOOGLE_SERVICE_ACCOUNT_JSON)
    if "private_key" in info:
        info["private_key"] = info["private_key"].replace("\\n", "\n")
    creds   = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
    service = build("drive", "v3", credentials=creds)
    return service


def natural_sort_key(s):
    """مفتاح ترتيب طبيعي (1, 2, 10 بدلاً من 1, 10, 2)."""
    return [int(text) if text.isdigit() else text.lower()
            for text in re.split('([0-9]+)', s)]


def _extract_id(url: str) -> Optional[str]:
    """استخراج الـ ID من روابط Drive المختلفة."""
    patterns = [
        r"drive\.google\.com/drive/folders/([a-zA-Z0-9_-]+)",
        r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)",
        r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)",
        r"drive\.google\.com/.*[?&]id=([a-zA-Z0-9_-]+)",
        r"docs\.google\.com/.*?/d/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]{25,})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    # Raw ID
    parts = url.strip("/").split("/")
    for part in reversed(parts):
        if len(part) >= 25 and re.match(r'^[a-zA-Z0-9_-]+$', part):
            return part
    return None


def _download_file(service, file_id: str, dest_path: str, progress_cb=None):
    """تحميل ملف من Drive باستخدام requests لتفادي مشاكل SSL في بعض بيئات التشغيل."""
    import requests
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    uri = request.uri
    
    headers = {}
    creds = getattr(service._http, "credentials", None)
    if creds:
        try:
            from google.auth.transport.requests import Request
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
        except Exception:
            pass
        creds.apply(headers)
        
    r = requests.get(uri, headers=headers, stream=True)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to download file {file_id}: HTTP {r.status_code} - {r.text[:500]}")
        
    total_size = int(r.headers.get('content-length', 0))
    downloaded = 0
    
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total_size > 0:
                    progress_cb(int((downloaded / total_size) * 100))


def _list_folder(service, folder_id: str) -> list:
    """قائمة الملفات في مجلد."""
    items = []
    page_token = None
    while True:
        kwargs = dict(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=200,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        if page_token:
            kwargs["pageToken"] = page_token
        result     = service.files().list(**kwargs).execute()
        items     += result.get("files", [])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    
    # ترتيب طبيعي في بايثون بدلاً من اعتماد Drive API
    items.sort(key=lambda x: natural_sort_key(x['name']))
    return items


async def stitch_from_drive(
    drive_url: str,
    title: str = "chapter",
    target_height: int = 14500,
    target_width: int  = 800,
    sensitivity: int   = 90,
    progress_callback: Optional[Callable] = None,
    output_dir: str = "temp_downloads",
) -> Optional[str]:
    """
    الدالة الرئيسية: تحميل من Drive وتطبيق SmartStitch.
    Returns: مسار ZIP الناتج أو None عند الفشل
    """
    loop    = asyncio.get_event_loop()
    job_id  = uuid.uuid4().hex[:8]
    work_dir = os.path.join(output_dir, f"drive_{job_id}")
    os.makedirs(work_dir, exist_ok=True)

    async def _pcb(pct: int, msg: str):
        if progress_callback:
            await progress_callback(pct, 100, msg)

    try:
        if not Config.GOOGLE_SERVICE_ACCOUNT_JSON:
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_JSON غير مضبوط — أضف Service Account في .env"
            )

        drive_id = _extract_id(drive_url)
        if not drive_id:
            raise ValueError(f"تعذّر استخراج Drive ID من: {drive_url}")

        await _pcb(2, "🔗 الاتصال بـ Google Drive...")

        def _fetch_meta():
            svc = _get_drive_service()
            meta = svc.files().get(
                fileId=drive_id,
                fields="id, name, mimeType",
                supportsAllDrives=True,
            ).execute()
            return svc, meta

        svc, meta = await loop.run_in_executor(None, _fetch_meta)
        mime = meta.get("mimeType", "")
        name = meta.get("name", title)

        image_paths: list[str] = []

        # ── مجلد → تحميل كل الصور بداخله ────────────────────────────────
        if mime == "application/vnd.google-apps.folder":
            await _pcb(5, f"📂 جلب قائمة الصور من مجلد: {name}")

            def _get_images():
                return [
                    f for f in _list_folder(svc, drive_id)
                    if any(f["name"].lower().endswith(ext) for ext in SUPPORTED_IMAGES)
                    or f["mimeType"].startswith("image/")
                ]

            files = await loop.run_in_executor(None, _get_images)
            if not files:
                raise ValueError("لم تُعثر على صور في المجلد")

            total = len(files)
            await _pcb(8, f"📥 تحميل {total} صورة من Drive...")

            def _dl_all():
                paths = []
                for i, f in enumerate(files):
                    ext = os.path.splitext(f["name"])[1] or ".jpg"
                    dest = os.path.join(work_dir, f"{i:04d}{ext}")
                    try:
                        _download_file(svc, f["id"], dest)
                        paths.append(dest)
                    except Exception as e:
                        print(f"[DriveStitch] skip {f['name']}: {e}")
                return sorted(paths)

            image_paths = await loop.run_in_executor(None, _dl_all)

        # ── ملف ZIP → فك الضغط ───────────────────────────────────────────
        elif mime == "application/zip" or name.lower().endswith(".zip"):
            await _pcb(5, f"📥 تحميل ملف ZIP: {name}")
            zip_path = os.path.join(work_dir, "source.zip")

            def _dl_zip():
                _download_file(svc, drive_id, zip_path)

            await loop.run_in_executor(None, _dl_zip)
            await _pcb(30, "📦 فك ضغط الملف...")

            def _extract():
                extract_dir = os.path.join(work_dir, "extracted")
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)
                imgs = []
                for root, _, fnames in os.walk(extract_dir):
                    # ترتيب طبيعي للملفات داخل المجلد
                    for fn in sorted(fnames, key=natural_sort_key):
                        if os.path.splitext(fn)[1].lower() in SUPPORTED_IMAGES:
                            imgs.append(os.path.join(root, fn))
                return imgs # المسارات مرتبة بالفعل

            image_paths = await loop.run_in_executor(None, _extract)

        # ── صورة واحدة مباشرة ─────────────────────────────────────────────
        elif mime.startswith("image/"):
            await _pcb(5, f"📥 تحميل الصورة: {name}")
            ext  = os.path.splitext(name)[1] or ".jpg"
            dest = os.path.join(work_dir, f"0001{ext}")

            def _dl_img():
                _download_file(svc, drive_id, dest)

            await loop.run_in_executor(None, _dl_img)
            image_paths = [dest]

        else:
            raise ValueError(f"نوع الملف غير مدعوم: {mime}")

        if not image_paths:
            raise ValueError("لم تُعثر على صور للمعالجة")

        await _pcb(40, f"🧵 تطبيق SmartStitch على {len(image_paths)} صورة...")

        stitch_out = os.path.join(work_dir, "stitched")

        safe_title = title.replace(" ", "_")

        def _run_stitch():
            return smart_stitch_to_files(
                image_paths=image_paths,
                output_dir=stitch_out,
                chapter_name=safe_title,
                target_height=target_height,
                target_width=target_width,
                sensitivity=sensitivity,
                output_format="jpg",
                output_quality=95,
            )

        stitched = await loop.run_in_executor(None, _run_stitch)
        if not stitched:
            raise ValueError("SmartStitch فشل في معالجة الصور")

        await _pcb(80, f"📦 ضغط {len(stitched)} قطعة في ZIP...")

        final_zip = os.path.join(output_dir, f"{safe_title}_stitched_{job_id}.zip")

        def _make_zip():
            with zipfile.ZipFile(final_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for f in stitched:
                    zf.write(f, os.path.basename(f))

        await loop.run_in_executor(None, _make_zip)
        await _pcb(100, f"✅ SmartStitch: {len(stitched)} قطعة جاهزة")

        return final_zip

    except Exception as e:
        print(f"[DriveStitch] Error: {e}")
        if progress_callback:
            await progress_callback(0, 100, f"❌ خطأ: {e}")
        return None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ──────────────────────────────────────────────
# Public Google Drive API Exports for Manga Cleaner
# ──────────────────────────────────────────────

def build_drive_service():
    return _get_drive_service()


def list_folder_images(service, folder_id: str) -> list:
    files = _list_folder(service, folder_id)
    return [f for f in files if os.path.splitext(f["name"])[1].lower() in SUPPORTED_IMAGES]


def download_file(service, file_id: str, dest_path: str):
    _download_file(service, file_id, dest_path)


def create_drive_folder(service, folder_name: str, parent_folder_id: str = None) -> str:
    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_folder_id:
        file_metadata["parents"] = [parent_folder_id]
        try:
            p_meta = service.files().get(fileId=parent_folder_id, fields="driveId", supportsAllDrives=True).execute()
            drive_id = p_meta.get("driveId")
            if drive_id:
                file_metadata["driveId"] = drive_id
        except Exception:
            pass
    
    file = service.files().create(
        body=file_metadata,
        fields="id",
        supportsAllDrives=True
    ).execute()
    return file.get("id")


def upload_file_to_drive(service, file_path: str, parent_folder_id: str, file_name: str) -> str:
    from googleapiclient.http import MediaFileUpload
    file_metadata = {
        "name": file_name,
        "parents": [parent_folder_id]
    }
    try:
        p_meta = service.files().get(fileId=parent_folder_id, fields="driveId", supportsAllDrives=True).execute()
        drive_id = p_meta.get("driveId")
        if drive_id:
            file_metadata["driveId"] = drive_id
    except Exception:
        pass

    mime_type = "image/png"
    if file_path.lower().endswith((".jpg", ".jpeg")):
        mime_type = "image/jpeg"
    elif file_path.lower().endswith(".webp"):
        mime_type = "image/webp"
        
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True
    ).execute()
    return file.get("id")
