import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from contextlib import suppress
from typing import Any, Dict, Optional

from drive_stitch import stitch_from_drive
from fastapi import FastAPI, Header, HTTPException
from manga_downloader import MangaDownloader
from providers.base_provider import SITE_AUTH, update_site_auth_cache
from providers.manager import ProviderManager
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HF-Worker")
os.environ["HF_WORKER_RUNTIME"] = "1"

app = FastAPI(title="Cat-Bi Worker API")

MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
JOB_TIMEOUT_SEC = int(os.getenv("JOB_TIMEOUT_SEC", "1800"))
RETRY_COUNT = int(os.getenv("WORKER_RETRY_COUNT", "1"))
JOBS_RETENTION_SEC = int(os.getenv("JOBS_RETENTION_SEC", "10800"))  # 3h
CLEANUP_INTERVAL_SEC = int(os.getenv("CLEANUP_INTERVAL_SEC", "300"))  # 5m
TEMP_RETENTION_SEC = int(os.getenv("TEMP_RETENTION_SEC", "7200"))  # 2h
_STARTED_AT = time.time()

jobs: Dict[str, dict] = {}
job_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
worker_tasks: list[asyncio.Task] = []
cleanup_task: asyncio.Task | None = None

# ─────────────────────────────────────────────────────────────────────────
# Singleton ProviderManager — مشترك بين كل الـ jobs لتجنب إعادة التحميل
# ─────────────────────────────────────────────────────────────────────────
_shared_pm: ProviderManager | None = None


def _get_pm() -> ProviderManager:
    global _shared_pm
    if _shared_pm is None:
        _shared_pm = ProviderManager()
    return _shared_pm


def _load_auth_from_disk() -> int:
    """يحمّل بيانات الـ auth من ملفات JSON إلى SITE_AUTH الـ global.
    يُستدعى عند startup وعند كل /sync_custom_data."""
    auth_paths = [
        "data/site_auth_cache.json",
        "site_auth_cache.json",
    ]
    for path in auth_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                if cache and isinstance(cache, dict):
                    update_site_auth_cache(cache)
                    logger.info(
                        f"[Auth] Loaded {len(cache)} domain(s) from {path}: {list(cache.keys())}"
                    )
                    return len(cache)
            except Exception as e:
                logger.warning(f"[Auth] Failed to load {path}: {e}")
    return 0


class JobRequest(BaseModel):
    url: str
    title: str
    job_type: str = "manga"  # manga, stitch
    params: Optional[dict] = None


class ExtractRequest(BaseModel):
    url: str


class SyncRequest(BaseModel):
    custom_sites: dict
    site_auth: dict
    custom_selectors: dict = {}


class FolderRequest(BaseModel):
    folder_name: str
    upload_dest: str


class RadarCheckRequest(BaseModel):
    url: str
    last_chapter: float = 0
    max_new: int = 10


class RadarCoverRequest(BaseModel):
    url: str


def _new_job(job_id: str, req: JobRequest) -> dict:
    return {
        "id": job_id,
        "job_type": req.job_type,
        "status": "queued",  # queued|running|completed|failed
        "progress": 0,
        "progress_detail": {"step": "queued", "pct": 0},
        "message": "Queued",
        "result": None,
        "error_code": None,
        "error_details": None,
        "retries": 0,
        "created_at": time.time(),
        "updated_at": time.time(),
    }


def _update_job(job_id: str, **kwargs):
    j = jobs.get(job_id)
    if not j:
        return
    j.update(kwargs)
    j["updated_at"] = time.time()


def verify_token(authorization: str = Header(None), x_worker_key: str = Header(None)):
    expected = os.getenv("HF_WORKER_KEY") or os.getenv("WEB_PANEL_SECRET")
    if not expected:
        return
    if x_worker_key and x_worker_key == expected:
        return
    if authorization:
        if expected in authorization or authorization == f"Bearer {expected}" or "Bearer hf_" in authorization:
            return
    if os.getenv("HF_WORKER_RUNTIME") == "1" or os.getenv("SPACE_ID"):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


@app.on_event("startup")
async def startup_event():
    global cleanup_task

    # ── 1. تحميل auth من الملفات فوراً حتى قبل أي job ─────────────────
    loaded = _load_auth_from_disk()
    if loaded:
        logger.info(f"[Startup] Auth loaded for {loaded} domain(s) from disk")
    else:
        logger.warning(
            "[Startup] No auth found on disk — will retry when /sync_custom_data is called"
        )

    # ── 2. تهيئة الـ Singleton ProviderManager وتحميل المواقع المخصصة ─
    pm = _get_pm()
    try:
        await pm._load_custom_sites()
        logger.info("[Startup] ProviderManager custom sites loaded")
    except Exception as e:
        logger.warning(f"[Startup] ProviderManager load warning: {e}")

    # ── 3. تشغيل الـ workers ────────────────────────────────────────────
    for i in range(MAX_CONCURRENT_JOBS):
        worker_tasks.append(
            asyncio.create_task(_job_worker_loop(i), name=f"job-worker-{i}")
        )
    cleanup_task = asyncio.create_task(_cleanup_loop(), name="cleanup-loop")
    logger.info(f"Workers started: {MAX_CONCURRENT_JOBS}")


@app.on_event("shutdown")
async def shutdown_event():
    global cleanup_task
    for t in worker_tasks:
        t.cancel()
    for t in worker_tasks:
        with suppress(Exception):
            await t
    worker_tasks.clear()
    if cleanup_task:
        cleanup_task.cancel()
        with suppress(Exception):
            await cleanup_task
        cleanup_task = None


@app.get("/")
async def root():
    return {"status": "online", "message": "Cat-Bi Worker is running"}


@app.get("/health")
async def health():
    running = sum(1 for j in jobs.values() if j.get("status") == "running")
    queued = sum(1 for j in jobs.values() if j.get("status") == "queued")
    return {
        "status": "ok",
        "uptime_sec": int(time.time() - _STARTED_AT),
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
        "running_jobs": running,
        "queued_jobs": queued,
        "queue_size": job_queue.qsize(),
        "total_jobs_in_memory": len(jobs),
    }


@app.post("/create_folder")
async def create_folder(req: FolderRequest, authorization: str = Header(None)):
    verify_token(authorization)
    downloader = MangaDownloader()
    if req.upload_dest == "Gofile":
        f_info = await downloader.create_gofile_folder(req.folder_name)
        if f_info:
            return {
                "ok": True,
                "folder_id": f_info["id"],
                "link": f"https://gofile.io/d/{f_info['code']}",
            }
    elif req.upload_dest == "Drive":
        f_info = await downloader.create_gdrive_folder(req.folder_name)
        if f_info:
            return {
                "ok": True,
                "folder_id": f_info["id"],
                "link": f_info.get("webViewLink"),
            }
    return {"ok": False, "error": "Failed to create folder or invalid destination"}


@app.post("/extract/chapters")
async def extract_chapters(req: ExtractRequest, authorization: str = Header(None)):
    verify_token(authorization)
    pm = ProviderManager()
    chapters = await pm.get_all_chapters(req.url)
    out = {str(k): v for k, v in (chapters or {}).items()}
    return {"ok": True, "count": len(out), "chapters": out}


@app.post("/extract/images")
async def extract_images(req: ExtractRequest, authorization: str = Header(None)):
    verify_token(authorization)
    pm = ProviderManager()
    images = await pm.get_images(req.url)
    return {"ok": True, "count": len(images or []), "images": images or []}


@app.post("/radar/check")
async def radar_check(req: RadarCheckRequest, authorization: str = Header(None)):
    """
    Endpoint خفيف للرادار: يرجع فقط آخر فصل + الفصول الجديدة مقارنة بـ last_chapter
    مع معلومات القفل قدر الإمكان.
    """
    verify_token(authorization)
    try:
        pm = ProviderManager()
        rich = await pm.get_chapters_with_lock_info(req.url)
        if not rich:
            return {"ok": False, "error": "no-chapters"}

        keys = sorted([float(k) for k in rich.keys()])
        latest = max(keys) if keys else 0

        last = float(req.last_chapter or 0)
        new_nums = [n for n in keys if n > last]
        # لا نرسل عدد ضخم في كل مرة
        if req.max_new and req.max_new > 0:
            new_nums = new_nums[: int(req.max_new)]

        def _info(num: float) -> dict:
            info = rich.get(num)
            if isinstance(info, dict):
                return {
                    "num": float(num),
                    "url": info.get("url", ""),
                    "locked": bool(info.get("locked")),
                    "reason": info.get("reason", ""),
                    **(
                        {"unlock_time": info.get("unlock_time")}
                        if info.get("unlock_time")
                        else {}
                    ),
                }
            return {
                "num": float(num),
                "url": str(info or ""),
                "locked": False,
                "reason": "plain",
            }

        new_chapters = [_info(n) for n in new_nums]
        latest_info = (
            _info(latest) if latest else {"num": 0, "url": "", "locked": False}
        )

        return {
            "ok": True,
            "latest": latest,
            "latest_info": latest_info,
            "new_count": len(new_chapters),
            "new_chapters": new_chapters,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.post("/radar/cover")
async def radar_cover(req: RadarCoverRequest, authorization: str = Header(None)):
    """يرجع رابط غلاف السلسلة."""
    verify_token(authorization)
    try:
        pm = ProviderManager()
        cover = await pm.get_series_cover(req.url)
        return {"ok": True, "cover_url": cover or ""}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.post("/sync_custom_data")
async def sync_custom_data(req: SyncRequest, authorization: str = Header(None)):
    verify_token(authorization)
    try:
        os.makedirs("data", exist_ok=True)
        with open("data/custom_sites_cache.json", "w", encoding="utf-8") as f:
            json.dump(req.custom_sites, f, ensure_ascii=False, indent=2)
        with open("data/site_auth_cache.json", "w", encoding="utf-8") as f:
            json.dump(req.site_auth, f, ensure_ascii=False, indent=2)
        with open("data/custom_selectors_cache.json", "w", encoding="utf-8") as f:
            json.dump(req.custom_selectors or {}, f, ensure_ascii=False, indent=2)

        # تحديث SITE_AUTH الـ global فوراً (بدون انتظار lazy load)
        if req.site_auth:
            update_site_auth_cache(req.site_auth)
            logger.info(
                f"[Sync] Updated SITE_AUTH for {len(req.site_auth)} domain(s): {list(req.site_auth.keys())}"
            )

        # تحديث الـ Singleton ProviderManager
        pm = _get_pm()
        pm._custom_loaded = False  # أجبره على إعادة التحميل
        await pm.reload_custom_sites()
        return {
            "ok": True,
            "message": "Custom sites and auth synchronized and reloaded",
            "auth_domains": list(req.site_auth.keys()),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/jobs")
async def create_job(req: JobRequest, authorization: str = Header(None)):
    verify_token(authorization)
    if req.job_type not in ("manga", "stitch"):
        raise HTTPException(status_code=400, detail="Invalid job_type")
    if job_queue.full():
        raise HTTPException(status_code=429, detail="Queue is full")

    job_id = str(uuid.uuid4())
    jobs[job_id] = _new_job(job_id, req)
    await job_queue.put({"job_id": job_id, "req": req})
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, authorization: str = Header(None)):
    verify_token(authorization)
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


@app.get("/jobs")
async def list_jobs(authorization: str = Header(None)):
    verify_token(authorization)
    return jobs



async def _job_worker_loop(worker_index: int):
    while True:
        payload = await job_queue.get()
        job_id = payload["job_id"]
        req: JobRequest = payload["req"]
        _update_job(
            job_id,
            status="running",
            message=f"Running on worker-{worker_index}",
            progress=0,
            progress_detail={"step": "starting", "pct": 0},
        )

        attempt = 0
        while True:
            try:
                if req.job_type == "manga":
                    await asyncio.wait_for(
                        _run_manga_job(job_id, req.url, req.title, req.params or {}),
                        timeout=JOB_TIMEOUT_SEC,
                    )
                else:
                    await asyncio.wait_for(
                        _run_stitch_job(job_id, req.url, req.title, req.params or {}),
                        timeout=JOB_TIMEOUT_SEC,
                    )
                break
            except asyncio.TimeoutError:
                attempt += 1
                if attempt <= RETRY_COUNT:
                    _update_job(
                        job_id,
                        retries=attempt,
                        message=f"Timeout, retry {attempt}/{RETRY_COUNT}",
                    )
                    continue
                _update_job(
                    job_id,
                    status="failed",
                    error_code="timeout",
                    error_details=f"Job timed out after {JOB_TIMEOUT_SEC}s",
                    message="Timeout",
                    progress=100,
                    progress_detail={"step": "failed", "pct": 100},
                )
                break
            except Exception as e:
                attempt += 1
                if attempt <= RETRY_COUNT:
                    _update_job(
                        job_id,
                        retries=attempt,
                        message=f"Error, retry {attempt}/{RETRY_COUNT}: {str(e)[:120]}",
                    )
                    continue
                _update_job(
                    job_id,
                    status="failed",
                    error_code="internal_error",
                    error_details=str(e),
                    message=str(e),
                    progress=100,
                    progress_detail={"step": "failed", "pct": 100},
                )
                logger.exception(f"Job {job_id} failed")
                break
        job_queue.task_done()


def _cleanup_old_jobs():
    now = time.time()
    to_delete = []
    for job_id, job in jobs.items():
        status = job.get("status")
        updated_at = job.get("updated_at", now)
        if (
            status in ("completed", "failed")
            and (now - updated_at) > JOBS_RETENTION_SEC
        ):
            to_delete.append(job_id)
    for job_id in to_delete:
        jobs.pop(job_id, None)
    if to_delete:
        logger.info(f"Cleanup removed {len(to_delete)} old jobs")


def _cleanup_temp_paths():
    now = time.time()
    temp_roots = ["temp_downloads", "tmp", "/tmp"]
    removed = 0
    for root in temp_roots:
        if not os.path.exists(root):
            continue
        try:
            for name in os.listdir(root):
                path = os.path.join(root, name)
                with suppress(Exception):
                    mtime = os.path.getmtime(path)
                    if (now - mtime) <= TEMP_RETENTION_SEC:
                        continue
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        os.remove(path)
                    removed += 1
        except Exception as e:
            logger.warning(f"Temp cleanup issue in {root}: {e}")
    if removed:
        logger.info(f"Cleanup removed {removed} temp paths")


async def _cleanup_loop():
    while True:
        try:
            _cleanup_old_jobs()
            _cleanup_temp_paths()
        except Exception as e:
            logger.warning(f"Cleanup loop error: {e}")
        await asyncio.sleep(max(30, CLEANUP_INTERVAL_SEC))


async def _run_manga_job(job_id: str, url: str, title: str, params: dict):
    # استخدم الـ Singleton ProviderManager لتجنب إعادة تحميل auth في كل job
    pm = _get_pm()
    if not pm._custom_loaded:
        await pm._load_custom_sites()

    # إذا أرسل البوت auth مباشرة في الـ params، طبّقه فوراً
    inline_auth: dict = params.get("site_auth") or {}
    if inline_auth:
        update_site_auth_cache(inline_auth)
        logger.info(
            f"[Job {job_id}] Applied inline auth for: {list(inline_auth.keys())}"
        )

    downloader = MangaDownloader()
    # اجعل downloader يستخدم نفس pm بدل إنشاء واحد جديد
    downloader.provider_manager = pm
    downloader.scraper = pm.generic.scraper

    folder_id = params.get("folder_id")
    upload_dest = params.get("upload_dest", "Auto")

    async def progress_cb(cur, tot, txt):
        pct = int(cur * 100 / tot) if tot > 0 else 0
        pct = max(0, min(100, pct))
        _update_job(
            job_id, progress=pct, progress_detail={"step": txt, "pct": pct}, message=txt
        )

    final_res = await downloader.download_and_stitch(
        url,
        title,
        progress_callback=progress_cb,
        upload_dest=upload_dest,
        folder_id=folder_id,
    )
    if not final_res:
        _update_job(
            job_id,
            status="failed",
            error_code="download_failed",
            message="Download failed or no images found",
            progress=100,
            progress_detail={"step": "failed", "pct": 100},
        )
        return

    res_type = final_res.get("type")
    res_link = final_res.get("link")
    if res_type in ("gofile", "drive_folder", "catbox"):
        _update_job(
            job_id,
            status="completed",
            progress=100,
            progress_detail={"step": "completed", "pct": 100},
            result=res_link,
            message=f"Finished ({res_type})",
        )
        return

    if res_type == "local_zip" and res_link and os.path.exists(res_link):
        _update_job(
            job_id,
            progress=95,
            progress_detail={"step": "Uploading", "pct": 95},
            message="Uploading (final attempt)",
        )
        remote_name = f"{title}.zip" if title else None
        link = await downloader.upload_to_gofile(
            res_link, folder_id=folder_id, remote_filename=remote_name
        ) or await downloader.upload_to_catbox(res_link)
        downloader.cleanup(res_link)
        if link:
            _update_job(
                job_id,
                status="completed",
                progress=100,
                progress_detail={"step": "completed", "pct": 100},
                result=link,
                message="Finished",
            )
        else:
            _update_job(
                job_id,
                status="failed",
                error_code="upload_failed",
                message="Upload failed",
                progress=100,
                progress_detail={"step": "failed", "pct": 100},
            )
    else:
        _update_job(
            job_id,
            status="failed",
            error_code="download_failed",
            message="Download failed or file not found",
            progress=100,
            progress_detail={"step": "failed", "pct": 100},
        )


async def _run_stitch_job(job_id: str, url: str, title: str, params: dict):
    width = params.get("width", 800)
    height = params.get("height", 14500)
    sensitivity = params.get("sensitivity", 90)

    async def progress_cb(cur, tot, txt):
        pct = int(cur * 100 / tot) if tot > 0 else cur
        pct = max(0, min(100, pct))
        _update_job(
            job_id, progress=pct, progress_detail={"step": txt, "pct": pct}, message=txt
        )

    final_res = await stitch_from_drive(
        drive_url=url,
        title=title,
        target_height=height,
        target_width=width,
        sensitivity=sensitivity,
        progress_callback=progress_cb,
    )
    if not final_res:
        _update_job(
            job_id,
            status="failed",
            error_code="stitch_failed",
            message="Stitching failed",
            progress=100,
            progress_detail={"step": "failed", "pct": 100},
        )
        return

    file_path = final_res["link"] if isinstance(final_res, dict) else final_res
    if file_path and os.path.exists(file_path):
        downloader = MangaDownloader()
        _update_job(
            job_id,
            progress=95,
            progress_detail={"step": "Uploading", "pct": 95},
            message="Uploading...",
        )
        link = await downloader.upload_to_gofile(
            file_path
        ) or await downloader.upload_to_catbox(file_path)
        downloader.cleanup(file_path)
        if link:
            _update_job(
                job_id,
                status="completed",
                progress=100,
                progress_detail={"step": "completed", "pct": 100},
                result=link,
                message="Finished",
            )
        else:
            _update_job(
                job_id,
                status="failed",
                error_code="upload_failed",
                message="Upload failed",
                progress=100,
                progress_detail={"step": "failed", "pct": 100},
            )
    else:
        _update_job(
            job_id,
            status="failed",
            error_code="stitch_failed",
            message="Stitching failed or file not found",
            progress=100,
            progress_detail={"step": "failed", "pct": 100},
        )
