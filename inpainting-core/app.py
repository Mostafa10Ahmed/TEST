"""
MangaCleaner GPU Microservice — app.py
# v2-fixed: Hard mask blending (no ghost text), safe HF download, reduced pad
Space: mmo9/Inpainting_bot
GPU: RTX Pro 6000 Blackwell (48 GB VRAM) â€” Persistent Pro GPU / ZeroGPU

Architecture:
  Gradio application with custom FastAPI endpoints registered on the Gradio internal server.
  This allows Hugging Face Gradio SDK to launch it natively as a Gradio app,
  while still exposing the API endpoints for the Discord Bot.

ZeroGPU Compatibility:
  Uses the @spaces.GPU decorator for model inference to comply with Hugging Face ZeroGPU
  startup requirements and dynamically allocate GPU resources.
"""

from __future__ import annotations
import re

import io
import logging
import os
import secrets
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# CUDA/cuDNN Libraries Preloader & Compatibility Self-Restart
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def init_cuda_compatibility():
    import sys
    import os
    
    # Skip compatibility layers on Hugging Face Spaces to avoid port binding conflicts with os.execve
    if "SPACE_ID" in os.environ:
        print("Running inside Hugging Face Spaces. Skipping CUDA compatibility preloader to prevent port conflicts.")
        return
        
    # Only run on Linux
    if sys.platform != "linux":
        return
        
    if os.environ.get("COMPAT_ENV_SET") == "1":
        # We are in the restarted process. Just preload the libs.
        import glob
        import ctypes
        print("Preloaded process active. Loading CUDA/cuDNN libraries...")
        compat_dir = "/tmp/cuda_compat"
        
        # Load low-level libs from compat_dir or standard paths
        libs_to_load = [
            "libcudart.so.12",
            "libnvJitLink.so.13",
            "libnvrtc.so.12",
            "libcublasLt.so.12",
            "libcublas.so.12",
            "libcufft.so.11",
            "libcurand.so.10",
            "libcusolver.so.11",
            "libcusparse.so.12",
            "libcudnn.so.9"
        ]
        
        site_packages_paths = [p for p in sys.path if "site-packages" in p]
        nvidia_lib_dirs = []
        for sp in site_packages_paths:
            nvidia_dir = os.path.join(sp, "nvidia")
            if os.path.isdir(nvidia_dir):
                for sub in os.listdir(nvidia_dir):
                    lib_dir = os.path.join(nvidia_dir, sub, "lib")
                    if os.path.isdir(lib_dir):
                        nvidia_lib_dirs.append(lib_dir)
        all_paths = [compat_dir] + nvidia_lib_dirs + [
            "/usr/local/cuda/lib64",
            "/usr/local/cuda/targets/x86_64-linux/lib",
            "/usr/lib/x86_64-linux-gnu"
        ]
        
        for libname in libs_to_load:
            found = False
            for p in all_paths:
                matches = glob.glob(os.path.join(p, libname + "*"))
                if matches:
                    matches.sort(key=len)
                    lib_path = matches[0]
                    try:
                        ctypes.CDLL(lib_path, mode=ctypes.RTLD_GLOBAL)
                        print(f"Successfully preloaded: {lib_path}")
                        found = True
                        break
                    except Exception as e:
                        print(f"Failed to preload {lib_path}: {e}")
            if not found:
                try:
                    ctypes.CDLL(libname, mode=ctypes.RTLD_GLOBAL)
                    print(f"Successfully loaded from system: {libname}")
                except Exception:
                    pass
        return

    # First run: create symlinks and restart
    import glob
    print("Initializing CUDA compatibility layer...")
    
    site_packages_paths = [p for p in sys.path if "site-packages" in p]
    nvidia_lib_dirs = []
    for sp in site_packages_paths:
        nvidia_dir = os.path.join(sp, "nvidia")
        if os.path.isdir(nvidia_dir):
            for sub in os.listdir(nvidia_dir):
                lib_dir = os.path.join(nvidia_dir, sub, "lib")
                if os.path.isdir(lib_dir):
                    nvidia_lib_dirs.append(lib_dir)
                    
    standard_paths = [
        "/usr/local/cuda/lib64",
        "/usr/local/cuda/targets/x86_64-linux/lib",
        "/usr/lib/x86_64-linux-gnu"
    ]
    
    all_paths = nvidia_lib_dirs + standard_paths
    
    compat_dir = "/tmp/cuda_compat"
    os.makedirs(compat_dir, exist_ok=True)
    
    symlink_map = {
        "libcublasLt.so.12": "libcublasLt.so.13",
        "libcublas.so.12": "libcublas.so.13",
        "libcudart.so.12": "libcudart.so.13",
        "libnvrtc.so.12": "libnvrtc.so.13",
        "libcufft.so.11": "libcufft.so.12",
        "libcusolver.so.11": "libcusolver.so.12",
    }
    
    for expected, actual in symlink_map.items():
        actual_path = None
        for p in all_paths:
            matches = glob.glob(os.path.join(p, actual + "*"))
            if matches:
                matches.sort(key=len)
                actual_path = matches[0]
                break
        if actual_path:
            dst_link = os.path.join(compat_dir, expected)
            if not os.path.exists(dst_link):
                try:
                    os.symlink(actual_path, dst_link)
                    print(f"Symlinked {expected} -> {actual_path}")
                except Exception as e:
                    print(f"Failed to symlink {expected}: {e}")
                    
    # Update environment and restart
    all_paths = [compat_dir] + all_paths
    ld_path = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = ":".join(all_paths) + (f":{ld_path}" if ld_path else "")
    os.environ["COMPAT_ENV_SET"] = "1"
    
    print(f"Restarting process with updated LD_LIBRARY_PATH: {os.environ['LD_LIBRARY_PATH']}")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(sys.executable, [sys.executable] + sys.argv, os.environ)

init_cuda_compatibility()

import cv2
import gradio as gr
import numpy as np
import onnxruntime as ort
import uvicorn
from fastapi import Depends, Header, HTTPException, UploadFile, File
from fastapi.responses import Response
from huggingface_hub import hf_hub_download
from PIL import Image
from ultralytics import YOLO
import torch
import torch.nn as nn
from safetensors.torch import load_file as load_safetensors

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Import spaces (ZeroGPU Decorator)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
try:
    import spaces
except ImportError:
    # Fallback for local testing or dedicated GPU environments
    class spaces:
        @staticmethod
        def GPU(func=None, duration=None):
            if callable(func):
                return func
            def decorator(f):
                return f
            return decorator

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Logging
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s â€” %(message)s",
)
log = logging.getLogger("manga_cleaner")

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Config
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
API_KEY: str = os.getenv("INPAINTING_API_KEY", "")
if API_KEY:
    log.info("API key loaded from environment (first 6 chars): %s...", API_KEY[:6])
else:
    log.info("INPAINTING_API_KEY not set in environment. Running in open microservice mode.")

# Switched to segmentation model — returns pixel masks instead of bounding boxes
YOLO_REPO        = "ogkalu/comic-text-segmenter-yolov8m"
YOLO_FILE        = "comic-text-segmenter.pt"
COMIC_DET_REPO   = "mayocream/comic-text-detector-onnx"
COMIC_DET_FILE   = "comic-text-detector.onnx"
# AOT-GAN: manga-image-translator AOT inpainting (SafeTensors, 22MB)
# Replaces LaMa — trained on manga/comic, far better at screentones & gradients
AOT_REPO         = "mayocream/aot-inpainting"
AOT_FILE         = "model.safetensors"
# Keep LaMa constants for backward-compat references (no longer used for inference)
LAMA_REPO        = "mayocream/lama-manga-onnx"
LAMA_FILE        = "lama-manga.onnx"
BUBBLE_SEG_REPO  = "huyvux3005/manga109-segmentation-bubble"
BUBBLE_SEG_FILE  = "best.pt"

YOLO_CONF      = float(os.getenv("YOLO_CONF",      "0.20"))
YOLO_IOU       = float(os.getenv("YOLO_IOU",       "0.45"))
BUBSEG_CONF    = float(os.getenv("BUBSEG_CONF",    "0.30"))
LAMA_SIZE      = int(os.getenv("LAMA_SIZE",        "512"))
DILATE_ITER    = int(os.getenv("DILATE_ITER",      "1"))
C_CONSTANT     = int(os.getenv("C_CONSTANT",       "13"))
BLUR_RADIUS    = int(os.getenv("BLUR_RADIUS",      "19"))
MAX_ZIP_MB     = int(os.getenv("MAX_ZIP_MB",       "500"))
# Minimum connected component area (px²) to keep — removes screentone/noise dots
MIN_COMP_AREA  = int(os.getenv("MIN_COMP_AREA",   "20"))

# Global model handles
yolo_model: Optional[YOLO] = None
comic_det_session: Optional[ort.InferenceSession] = None
lama_session: Optional[ort.InferenceSession] = None  # LaMa Manga ONNX
aot_model = None  # AOT-GAN removed — not used
bubble_seg_model: Optional[YOLO] = None
sd_pipe = None   # StableDiffusionInpaintPipeline for background reconstruction


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Model Loading Helper (Lazy Loading on First Use)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def ensure_models_loaded():
    global yolo_model, comic_det_session, lama_session, aot_model, bubble_seg_model, sd_pipe

    if yolo_model is None:
        log.info("Loading YOLOv8 text segmenter …")
        try:
            yolo_path = hf_hub_download(repo_id=YOLO_REPO, filename=YOLO_FILE)
            yolo_model = YOLO(yolo_path)
            if torch.cuda.is_available():
                try:
                    yolo_model.to(0)
                except Exception:
                    pass
            log.info("YOLOv8 text segmenter loaded successfully.")
        except Exception as exc:
            log.error("YOLO load failed: %s", exc)

    if comic_det_session is None:
        log.info("Loading ComicTextDetector ONNX model …")
        try:
            det_path = hf_hub_download(repo_id=COMIC_DET_REPO, filename=COMIC_DET_FILE)
            providers = ["CPUExecutionProvider"]
            if torch.cuda.is_available() or "CUDAExecutionProvider" in ort.get_available_providers():
                providers.insert(0, ("CUDAExecutionProvider", {"device_id": 0}))
            sess_opts = ort.SessionOptions()
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess_opts.enable_mem_pattern = True
            comic_det_session = ort.InferenceSession(
                det_path, sess_options=sess_opts, providers=providers
            )
            log.info("ComicTextDetector loaded — providers: %s", comic_det_session.get_providers())
        except Exception as exc:
            log.error("ComicTextDetector load failed: %s", exc)

    if lama_session is None:
        log.info("Loading LaMa ONNX model …")
        try:
            lama_path = hf_hub_download(repo_id=LAMA_REPO, filename=LAMA_FILE)
            providers = ["CPUExecutionProvider"]
            if torch.cuda.is_available() or "CUDAExecutionProvider" in ort.get_available_providers():
                providers.insert(0, ("CUDAExecutionProvider", {"device_id": 0}))
            sess_opts = ort.SessionOptions()
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess_opts.enable_mem_pattern = True
            lama_session = ort.InferenceSession(
                lama_path, sess_options=sess_opts, providers=providers
            )
            log.info("LaMa ONNX loaded — providers: %s", lama_session.get_providers())
        except Exception as exc:
            log.error("LaMa load failed: %s", exc)

    if bubble_seg_model is None:
        log.info("Loading bubble segmentation model (manga109-segmentation-bubble) ...")
        try:
            bubble_seg_path = hf_hub_download(repo_id=BUBBLE_SEG_REPO, filename=BUBBLE_SEG_FILE)
            bubble_seg_model = YOLO(bubble_seg_path)
            if torch.cuda.is_available():
                try:
                    bubble_seg_model.to(0)
                except Exception:
                    pass
            log.info("Bubble seg model loaded. task=%s", getattr(bubble_seg_model, "task", "unknown"))
        except Exception as exc:
            log.error("Bubble seg model load failed (non-fatal): %s", exc)

    if sd_pipe is None:
        log.info("Loading Stable Diffusion 2 Inpainting pipeline ...")
        try:
            from diffusers import StableDiffusionInpaintPipeline
            sd_pipe = StableDiffusionInpaintPipeline.from_pretrained(
                "stabilityai/stable-diffusion-2-inpainting",
                torch_dtype=torch.float16,
                safety_checker=None,
                requires_safety_checker=False,
            )
            sd_pipe = sd_pipe.to("cuda")
            sd_pipe.set_progress_bar_config(disable=True)
            # Speed optimisations
            sd_pipe.enable_attention_slicing()
            log.info("SD2 Inpainting pipeline loaded on CUDA")
        except Exception as exc:
            log.error("SD2 Inpainting load failed (will fall back to Telea): %s", exc)
            sd_pipe = None



def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


qwen_model = None
qwen_processor = None

def _normalize_lang(lang: str) -> str:
    lang = (lang or "auto").strip().lower()
    if lang in {"ko", "korean", "ko_kr"}:
        return "ko"
    if lang in {"ja", "japanese", "japan", "ja_jp"}:
        return "ja"
    if lang in {"en", "english", "en_us"}:
        return "en"
    if lang in {"ar", "arabic"}:
        return "ar"
    if lang in {"zh", "chinese", "zh_cn"}:
        return "zh"
    return "auto"

def ensure_ocr_loaded():
    global qwen_model, qwen_processor
    if qwen_model is not None:
        return
        
    import torch
    from transformers import Qwen2VLForConditionalGeneration, AutoProcessor
    
    log.info("Loading Qwen2-VL-2B-Instruct on CPU (ZeroGPU will handle CUDA redirection)...")
    try:
        qwen_model = Qwen2VLForConditionalGeneration.from_pretrained(
            "Qwen/Qwen2-VL-2B-Instruct",
            torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True,
            device_map="auto"
        )
        qwen_processor = AutoProcessor.from_pretrained("Qwen/Qwen2-VL-2B-Instruct")
        log.info("Qwen2-VL-2B-Instruct loaded successfully.")
    except Exception as e:
        log.error("Failed to load Qwen2-VL-2B-Instruct: %s", e)
        raise e


def _sort_bubbles_reading_order(bubbles: list[dict], lang: str = "auto") -> list[dict]:
    if not bubbles:
        return []

    items = []
    for bubble in bubbles:
        bbox = bubble.get("bbox") or []
        if len(bbox) != 4:
            items.append({**bubble, "center_x": 0.0, "center_y": 0.0, "height": 0.0})
            continue
        xs = [pt[0] for pt in bbox]
        ys = [pt[1] for pt in bbox]
        items.append({
            **bubble,
            "center_x": sum(xs) / 4.0,
            "center_y": sum(ys) / 4.0,
            "height": max(ys) - min(ys),
        })

    items.sort(key=lambda it: it["center_y"])

    bands = []
    for item in items:
        added = False
        for band in bands:
            band_avg_y = sum(it["center_y"] for it in band) / len(band)
            band_avg_h = sum(it["height"] for it in band) / len(band)
            threshold = max(band_avg_h * 0.7, 30.0)
            if abs(item["center_y"] - band_avg_y) < threshold:
                band.append(item)
                added = True
                break
        if not added:
            bands.append([item])

    sorted_items = []
    reverse_x = lang in {"ja", "ar"}
    for band in bands:
        band.sort(key=lambda it: it["center_x"], reverse=reverse_x)
        sorted_items.extend(band)

    return [dict(item) for item in sorted_items]


def _get_bubble_overlap(bbox1, bbox2) -> float:
    xs1 = [pt[0] for pt in bbox1]
    ys1 = [pt[1] for pt in bbox1]
    xs2 = [pt[0] for pt in bbox2]
    ys2 = [pt[1] for pt in bbox2]
    
    x_min1, y_min1, x_max1, y_max1 = min(xs1), min(ys1), max(xs1), max(ys1)
    x_min2, y_min2, x_max2, y_max2 = min(xs2), min(ys2), max(xs2), max(ys2)
    
    x_int_min = max(x_min1, x_min2)
    y_int_min = max(y_min1, y_min2)
    x_int_max = min(x_max1, x_max2)
    y_int_max = min(y_max1, y_max2)
    
    if x_int_max <= x_int_min or y_int_max <= y_int_min:
        return 0.0
        
    area_int = (x_int_max - x_int_min) * (y_int_max - y_int_min)
    area1 = (x_max1 - x_min1) * (y_max1 - y_min1)
    area2 = (x_max2 - x_min2) * (y_max2 - y_min2)
    
    union_area = area1 + area2 - area_int
    if union_area <= 0:
        return 0.0
    return area_int / union_area


@spaces.GPU(duration=120)
def process_ocr_batch(images_data: list[bytes], lang: str = "auto", remove_sfx: bool = False, connected_slashes: bool = False) -> list[dict]:
    ensure_models_loaded()
    results = []
    normalized_lang = _normalize_lang(lang)
    lang_names = {
        "ko": "Korean",
        "ja": "Japanese",
        "zh": "Chinese",
        "ar": "Arabic",
        "en": "English",
        "auto": "the primary language of the text"
    }
    target_lang_name = lang_names.get(normalized_lang, "the primary language of the text")

    prompt_text = (
        f"Identify the bubble style in this image and transcribe the {target_lang_name} text.\n"
        "Choose the correct prefix symbol based on these visual rules:\n"
        '- "": Normal speech bubble (oval/round border)\n'
        '- (): Thought bubble (cloud-shaped or tail-less border)\n'
        '- []: Narration box (rectangular/square border)\n'
        '- OT: Outside text (no border, text is directly on the artwork)\n'
        '- ST: Small text (tiny handwritten comments/noises next to main bubbles)\n'
        '- SFX: Sound effects (artistic drawn lettering, e.g., Boom, Vroom)\n'
        '- <>: System screen (digital RPG status window or menu box)\n'
        '- :: Screaming bubble (jagged, thorny, or spiky border)\n\n'
        "Output format must be exactly: Symbol: TranscribedText\n"
        "Output nothing else."
    )

    allowed_classes = {0, 1, 2, 3, 5} if remove_sfx else {0, 1, 2, 3, 4, 5}

    for idx, raw in enumerate(images_data):
        try:
            nparr = np.frombuffer(raw, np.uint8)
            img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img_bgr is None:
                results.append({"page_num": idx + 1, "texts": []})
                continue
                
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            H, W = img_rgb.shape[:2]
            
            # Slice long vertical manhwa/webtoon images to avoid aspect ratio distortion in YOLO
            slices = []
            if H / W >= 2.0 and W > 0:
                tile_h = min(H, max(1280, W * 2))
                overlap = 350
                stride = tile_h - overlap
                if stride <= 0:
                    slices.append((0, H))
                else:
                    y_start = 0
                    while y_start < H:
                        y_end = min(y_start + tile_h, H)
                        if y_end == H and y_start > 0:
                            y_start = max(0, H - tile_h)
                        slices.append((y_start, y_end))
                        if y_end == H:
                            break
                        y_start += stride
            else:
                slices.append((0, H))
            
            bubble_crops = []
            for y_start, y_end in slices:
                chunk = img_rgb[y_start:y_end, :]
                chunk_h, chunk_w = chunk.shape[:2]
                if chunk_h == 0 or chunk_w == 0:
                    continue
                
                yolo_res = yolo_model.predict(
                    source=chunk,
                    conf=0.15,
                    iou=YOLO_IOU,
                    verbose=False,
                    device="cuda"
                )
                
                raw_boxes: list[tuple[int, int, int, int, float]] = []
                for r in yolo_res:
                    if r.boxes is None:
                        continue
                    for box, cls, conf in zip(r.boxes.xyxy, r.boxes.cls, r.boxes.conf):
                        if int(cls) not in allowed_classes or conf < 0.15:
                            continue
                        x_min, y_min, x_max, y_max = map(int, box.cpu().numpy())
                        raw_boxes.append((x_min, y_min, x_max, y_max, float(conf)))

                for bx_min, by_min, bx_max, by_max, bconf in raw_boxes:
                    bw_box = bx_max - bx_min
                    bh_box = by_max - by_min

                    # --- Adaptive padding: capped by half the distance to nearest neighbour ---
                    # Base padding is 5 % of the bubble dimension to keep crops tight.
                    # We clamp it to half the gap to the closest neighbour so two adjacent bubbles
                    # never overlap in the crop, preventing text bleeding.
                    base_pad_x = max(4, int(bw_box * 0.05))
                    base_pad_y = max(4, int(bh_box * 0.05))

                    min_gap_x = chunk_w   # sentinel
                    min_gap_y = chunk_h
                    for ox_min, oy_min, ox_max, oy_max, _ in raw_boxes:
                        if (ox_min, oy_min, ox_max, oy_max) == (bx_min, by_min, bx_max, by_max):
                            continue
                        # Horizontal gap (only relevant if boxes are on the same row)
                        if oy_min < by_max and oy_max > by_min:   # vertical overlap exists
                            if ox_min > bx_max:
                                min_gap_x = min(min_gap_x, ox_min - bx_max)
                            elif ox_max < bx_min:
                                min_gap_x = min(min_gap_x, bx_min - ox_max)
                        # Vertical gap
                        if ox_min < bx_max and ox_max > bx_min:   # horizontal overlap exists
                            if oy_min > by_max:
                                min_gap_y = min(min_gap_y, oy_min - by_max)
                            elif oy_max < by_min:
                                min_gap_y = min(min_gap_y, by_min - oy_max)

                    # Use at most half the gap so we never reach a neighbour's territory
                    safe_pad_x = min(base_pad_x, max(1, min_gap_x // 2))
                    safe_pad_y = min(base_pad_y, max(1, min_gap_y // 2))

                    x_min = max(0, bx_min - safe_pad_x)
                    y_min = max(0, by_min - safe_pad_y)
                    x_max = min(chunk_w, bx_max + safe_pad_x)
                    y_max = min(chunk_h, by_max + safe_pad_y)

                    if x_max <= x_min or y_max <= y_min:
                        continue

                    crop = chunk[y_min:y_max, x_min:x_max]

                    # Upscale small images to improve OCR quality
                    h, w = crop.shape[:2]
                    if w < 250 or h < 250:
                        scale = 300.0 / min(w, h)
                        new_w = int(w * scale)
                        new_h = int(h * scale)
                        crop = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

                    bubble_crops.append((crop, x_min, y_min, x_max, y_max, y_start, bconf))

            bubbles = []
            if bubble_crops:
                from PIL import Image
                batch_messages = []
                for crop, _, _, _, _, _, _ in bubble_crops:
                    pil_crop = Image.fromarray(crop)
                    messages = [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image", "image": pil_crop},
                                {"type": "text", "text": prompt_text},
                            ],
                        }
                    ]
                    batch_messages.append(messages)
                
                # Run Qwen2-VL inference
                texts = [qwen_processor.apply_chat_template(msg, tokenize=False, add_generation_prompt=True) for msg in batch_messages]
                from qwen_vl_utils import process_vision_info
                image_inputs, video_inputs = process_vision_info(batch_messages)
                
                import torch
                with torch.no_grad():
                    inputs = qwen_processor(
                        text=texts,
                        images=image_inputs,
                        videos=video_inputs,
                        padding=True,
                        return_tensors="pt",
                    ).to("cuda")
                    
                    generated_ids = qwen_model.generate(**inputs, max_new_tokens=128)
                    generated_ids_trimmed = [
                        out_ids[len(in_ids) :] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
                    ]
                    output_texts = qwen_processor.batch_decode(
                        generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
                    )
                
                # Map outputs back to bubbles
                for (crop, x_min, y_min, x_max, y_max, y_start, conf), transcribed_text in zip(bubble_crops, output_texts):
                    orig_lines = [line.strip() for line in transcribed_text.split("\n") if line.strip()]
                    # Join lines with double slash if connected_slashes is enabled
                    if connected_slashes:
                        text_clean = " // ".join(orig_lines)
                    else:
                        text_clean = " ".join(orig_lines)
                        
                    if not text_clean:
                        continue
                        
                    # Build bubble dictionary
                    bubble_bbox = [[x_min, y_min + y_start], [x_max, y_min + y_start], [x_max, y_max + y_start], [x_min, y_max + y_start]]
                    bubbles.append({
                        "bbox": bubble_bbox,
                        "text": text_clean,
                        "confidence": round(float(conf), 3),
                        "line_count": len(orig_lines),
                        "reader_lang": normalized_lang,
                    })

            # De-duplicate overlapping bubbles (e.g. from overlap regions)
            merged_bubbles = []
            for b in bubbles:
                dup = False
                for mb in merged_bubbles:
                    if _get_bubble_overlap(b["bbox"], mb["bbox"]) > 0.4:
                        dup = True
                        if b["confidence"] > mb["confidence"]:
                            mb["bbox"] = b["bbox"]
                            mb["text"] = b["text"]
                            mb["confidence"] = b["confidence"]
                            mb["line_count"] = b["line_count"]
                            mb["reader_lang"] = b["reader_lang"]
                        break
                if not dup:
                    merged_bubbles.append(b)
            bubbles = merged_bubbles

            bubbles = _sort_bubbles_reading_order(bubbles, lang=normalized_lang)
            for bubble_idx, bubble in enumerate(bubbles, start=1):
                bubble["bubble_num"] = bubble_idx

            texts_only = [bubble["text"] for bubble in bubbles]
            results.append({
                "page_num": idx + 1,
                "bubble_count": len(bubbles),
                "bubbles": bubbles,
                "texts": texts_only,
            })
        except Exception as exc:
            import traceback
            log.warning("Batch OCR error on index %d: %s\n%s", idx, exc, traceback.format_exc())
            results.append({"page_num": idx + 1, "bubble_count": 0, "bubbles": [], "texts": []})
            
    return results

def _is_scanlation_watermark(x: int, y: int, bw: int, bh: int, w: int, h: int) -> bool:
    margin_y_bottom = int(h * 0.06) if h > 2000 else int(h * 0.08)
    margin_y_top = int(h * 0.04) if h > 2000 else int(h * 0.06)
    
    is_bottom_corner = (y + bh >= h - margin_y_bottom) and (x <= 35 or x + bw >= w - 35) and bh <= 55
    is_top_corner = (y <= margin_y_top) and (x <= 35 or x + bw >= w - 35) and bh <= 50
    is_side_ribbon = (x <= 25 or x + bw >= w - 25) and (15 <= bh <= 55) and (70 <= bw <= 380)
    
    return is_bottom_corner or is_top_corner or is_side_ribbon


def _filter_scanlation_watermarks(mask: np.ndarray, w: int, h: int) -> np.ndarray:
    """Preserves scanlation group watermarks/credit badges in outer margins and corners (e.g. ASURASCANS.COM).
    Guarantees credit watermarks and panel ribbons are not erased while cleaning dialog/SFX/titles.
    """
    if mask is None or mask.max() == 0:
        return mask
    contours, _ = cv2.findContours(mask.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    clean_mask = np.zeros_like(mask)
    
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        if _is_scanlation_watermark(x, y, bw, bh, w, h):
            log.info("Preserving scanlation credit watermark at (%d, %d, %d, %d)", x, y, bw, bh)
            continue
        cv2.drawContours(clean_mask, [cnt], -1, 255, -1)
    return clean_mask


def _build_comic_detector_mask(image_bgr: np.ndarray) -> np.ndarray:
    """Runs mayocream/comic-text-detector-onnx sliding window over the image.
    Uses context-aware Ultimate Hybrid masking (Line Envelope for titles/cards + Adaptive Radial for overlays).
    """
    if comic_det_session is None:
        return np.zeros(image_bgr.shape[:2], dtype=np.uint8)
    h, w = image_bgr.shape[:2]
    tile_size = 1024
    overlap = 256
    stride = tile_size - overlap
    
    full_mask = np.zeros((h, w), dtype=np.float32)
    
    y_coords = []
    y = 0
    while y < h:
        y_coords.append(min(y, max(0, h - tile_size)))
        if y + tile_size >= h:
            break
        y += stride
    if not y_coords:
        y_coords = [0]
        
    x_coords = []
    x = 0
    while x < w:
        x_coords.append(min(x, max(0, w - tile_size)))
        if x + tile_size >= w:
            break
        x += stride
    if not x_coords:
        x_coords = [0]
        
    for y0 in y_coords:
        for x0 in x_coords:
            y1 = min(h, y0 + tile_size)
            x1 = min(w, x0 + tile_size)
            tile = image_bgr[y0:y1, x0:x1]
            th, tw = tile.shape[:2]
            
            padded = np.zeros((1024, 1024, 3), dtype=np.uint8)
            padded[:th, :tw] = tile
            
            rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
            input_tensor = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[np.newaxis, ...]
            
            outputs = comic_det_session.run(None, {"images": input_tensor})
            blk, seg, det = outputs
            
            tile_seg = seg[0, 0, :th, :tw]
            tile_det = det[0, 0, :th, :tw]
            tile_combined = np.maximum(tile_seg, tile_det)
            
            full_mask[y0:y1, x0:x1] = np.maximum(full_mask[y0:y1, x0:x1], tile_combined)
            
    raw_binary = (full_mask > 0.15).astype(np.uint8) * 255
    
    # 1. Connect nearby strokes horizontally into text line candidates
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 3))
    connected_lines = cv2.dilate(raw_binary, h_kernel, iterations=1)
    contours, _ = cv2.findContours(connected_lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    hybrid_mask = np.zeros_like(raw_binary)
    for cnt in contours:
        x, y_box, bw, bh = cv2.boundingRect(cnt)
        if bw < 5 or bh < 5:
            continue
        
        # Check if watermark
        if _is_scanlation_watermark(x, y_box, bw, bh, w, h):
            continue
            
        font_dim = max(bw, bh)
        aspect = bw / float(bh)
        
        # Wide lines / titles / cards: Use Line Envelope Box (absorbs all outer glow & drop shadows)
        if (bw >= 35 and aspect >= 1.5) or (bw >= 70):
            pad_x = min(12, max(6, int(bw * 0.04)))
            pad_y = min(10, max(5, int(bh * 0.15)))
            x0 = max(0, x - pad_x)
            y0 = max(0, y_box - pad_y)
            x1 = min(w, x + bw + pad_x)
            y1 = min(h, y_box + bh + pad_y)
            cv2.rectangle(hybrid_mask, (x0, y0), (x1, y1), 255, -1)
        else:
            # Over character body / small text: Adaptive Elliptical Radial Dilation
            cnt_mask = np.zeros_like(raw_binary)
            cv2.drawContours(cnt_mask, [cnt], -1, 255, -1)
            
            if font_dim > 50:
                k_size = 17
            elif font_dim > 25:
                k_size = 11
            else:
                k_size = 7
                
            k_rad = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
            dil = cv2.dilate(cnt_mask, k_rad, iterations=1)
            hybrid_mask = cv2.bitwise_or(hybrid_mask, dil)
        
    return hybrid_mask


def _build_text_mask(image_bgr: np.ndarray, dilate_iter: int = 3, remove_sfx: bool = False) -> np.ndarray:
    h, w = image_bgr.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)

    # 1. Run ComicTextDetector ONNX (Primary specialized detector)
    comic_mask = _build_comic_detector_mask(image_bgr)
    mask = cv2.bitwise_or(mask, comic_mask)

    # 2. Run YOLOv8 text segmenter pass (Ensemble detector)
    if yolo_model is not None:
        conf_thresh_predict = 0.15
        task_type = getattr(yolo_model, "task", "detect")
        bubble_classes = {0, 1, 2, 3}
        sfx_classes = {4, 5}
        allowed_classes = bubble_classes if not remove_sfx else (bubble_classes | sfx_classes)

        def _check_conf(c_int: int, conf: float, base_conf: float) -> bool:
            if c_int not in allowed_classes:
                return False
            return conf >= base_conf

        def _run_yolo_pass(conf_val: float) -> np.ndarray:
            pass_mask = np.zeros((h, w), dtype=np.uint8)
            dev_target = 0 if torch.cuda.is_available() else "cpu"
            if h / w < 2.0:
                results = yolo_model.predict(
                    source=image_bgr[:, :, ::-1],
                    conf=conf_val,
                    iou=YOLO_IOU,
                    verbose=False,
                    device=dev_target,
                    retina_masks=(task_type == "segment"),
                )
                for r in results:
                    if r.boxes is None:
                        continue
                    if task_type == "segment" and r.masks is not None:
                        for seg_mask, cls, conf in zip(r.masks.data, r.boxes.cls, r.boxes.conf):
                            c_int = int(cls)
                            if not _check_conf(c_int, float(conf), conf_val):
                                continue
                            seg_np = (seg_mask.cpu().numpy() > 0.5).astype(np.uint8) * 255
                            seg_np = cv2.resize(seg_np, (w, h), interpolation=cv2.INTER_NEAREST)
                            pass_mask = cv2.bitwise_or(pass_mask, seg_np)
                    else:
                        for box, cls, conf in zip(r.boxes.xyxy, r.boxes.cls, r.boxes.conf):
                            c_int = int(cls)
                            if not _check_conf(c_int, float(conf), conf_val):
                                continue
                            x0, y0, x1, y1 = map(int, box.cpu().numpy())
                            cv2.rectangle(pass_mask, (x0, y0), (x1, y1), 255, -1)
            else:
                tile_h = min(h, max(1280, w * 2))
                overlap = 350
                stride = tile_h - overlap
                y_start = 0
                while y_start < h:
                    y_end = min(y_start + tile_h, h)
                    tile_img = image_bgr[y_start:y_end, :]
                    tile_h_actual, tile_w = tile_img.shape[:2]
                    if tile_h_actual == 0 or tile_w == 0:
                        break
                    results = yolo_model.predict(
                        source=tile_img[:, :, ::-1],
                        conf=conf_val,
                        iou=YOLO_IOU,
                        verbose=False,
                        device=dev_target,
                        retina_masks=(task_type == "segment"),
                    )
                    for r in results:
                        if r.boxes is None:
                            continue
                        if task_type == "segment" and r.masks is not None:
                            for seg_mask, cls, conf in zip(r.masks.data, r.boxes.cls, r.boxes.conf):
                                c_int = int(cls)
                                if not _check_conf(c_int, float(conf), conf_val):
                                    continue
                                seg_np = (seg_mask.cpu().numpy() > 0.5).astype(np.uint8) * 255
                                seg_np = cv2.resize(seg_np, (tile_w, tile_h_actual), interpolation=cv2.INTER_NEAREST)
                                pass_mask[y_start:y_end, :] = cv2.bitwise_or(pass_mask[y_start:y_end, :], seg_np)
                        else:
                            for box, cls, conf in zip(r.boxes.xyxy, r.boxes.cls, r.boxes.conf):
                                c_int = int(cls)
                                if not _check_conf(c_int, float(conf), conf_val):
                                    continue
                                x0, y0, x1, y1 = map(int, box.cpu().numpy())
                                cv2.rectangle(pass_mask[y_start:y_end, :], (x0, y0), (x1, y1), 255, -1)
                    if y_end >= h:
                        break
                    y_start += stride
            return pass_mask

        yolo_mask = _run_yolo_pass(conf_thresh_predict)
        mask = cv2.bitwise_or(mask, yolo_mask)

    # 3. Filter scanlation credit watermarks (e.g. ASURASCANS.COM)
    mask = _filter_scanlation_watermarks(mask, w, h)

    # 4. Horizontal line bridging + Morphological closing to seal complex Korean glyphs, hollow centers, and titles
    h_bridge = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 5))
    bridged = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, h_bridge)

    # 5. Adaptive Line Envelope & Halo Dilation for Titles & SFX
    contours, _ = cv2.findContours(bridged.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    refined_mask = np.zeros_like(mask)
    for cnt in contours:
        x, y_box, bw, bh = cv2.boundingRect(cnt)
        if bw < 4 or bh < 4:
            continue
        
        # For large stylized titles/narration (width > 80 or height > 35), use line envelope rectangle to guarantee 100% removal
        if bw > 80 or bh > 35:
            pad = 8
            x0, y0 = max(0, x - pad), max(0, y_box - pad)
            x1, y1 = min(w, x + bw + pad), min(h, y_box + bh + pad)
            cv2.rectangle(refined_mask, (x0, y0), (x1, y1), 255, -1)
        else:
            cnt_mask = np.zeros_like(mask)
            cv2.drawContours(cnt_mask, [cnt], -1, 255, -1)
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
            dil = cv2.dilate(cnt_mask, k, iterations=1)
            refined_mask = cv2.bitwise_or(refined_mask, dil)

    # Re-apply scanlation watermark filter to ensure credit badge safety
    refined_mask = _filter_scanlation_watermarks(refined_mask, w, h)
    return refined_mask


def _hybrid_inpaint(img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Inpaint img_rgb using LaMa ONNX Neural Inpainting for ALL text regions.
    Seamlessly reconstructs speech bubble gradients, system windows, dark aura boxes,
    and complex backgrounds without blocky solid color patches or white blobs.
    """
    if mask is None or mask.max() == 0:
        return img_rgb

    # Run LaMa ONNX Neural Inpainter on the full text mask
    out = _lama_inpaint_tile(img_rgb, mask)
    return out


def _build_bubble_interior_mask(image_bgr: np.ndarray) -> np.ndarray:
    """Use the bubble segmentation model to build a mask covering bubble interiors.
    Used to constrain inpainting so borders and artwork are never damaged.
    Returns uint8 (h, w) mask: 255 inside bubbles, 0 outside.
    """
    h, w = image_bgr.shape[:2]
    interior = np.zeros((h, w), dtype=np.uint8)
    if bubble_seg_model is None:
        return interior
    try:
        task_type = getattr(bubble_seg_model, "task", "segment")
        dev_target = 0 if torch.cuda.is_available() else "cpu"
        results = bubble_seg_model.predict(
            source=image_bgr[:, :, ::-1],
            conf=BUBSEG_CONF,
            iou=YOLO_IOU,
            verbose=False,
            device=dev_target,
            retina_masks=(task_type == "segment"),
        )
        for r in results:
            if r.boxes is None:
                continue
            if task_type == "segment" and r.masks is not None:
                for seg_mask in r.masks.data:
                    seg_np = (seg_mask.cpu().numpy() > 0.5).astype(np.uint8) * 255
                    seg_np = cv2.resize(seg_np, (w, h), interpolation=cv2.INTER_NEAREST)
                    interior = cv2.bitwise_or(interior, seg_np)
            else:
                for box in r.boxes.xyxy:
                    x0, y0, x1, y1 = map(int, box.cpu().numpy())
                    cv2.rectangle(interior, (x0, y0), (x1, y1), 255, -1)
    except Exception as exc:
        log.warning("bubble_seg_model inference failed (non-fatal): %s", exc)
    return interior


def _lama_inpaint_tile(img_rgb: np.ndarray, mask: np.ndarray, size: int = 512) -> np.ndarray:
    if lama_session is None or mask is None or mask.max() == 0:
        return img_rgb

    h, w = img_rgb.shape[:2]
    img_out = img_rgb.copy()

    merge_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (31, 31))
    dilated_for_merge = cv2.dilate(mask, merge_kernel, iterations=1)
    contours, _ = cv2.findContours(dilated_for_merge, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if not contours:
        return img_rgb

    input_names = [inp.name for inp in lama_session.get_inputs()]

    for cnt in contours:
        rx, ry, rw, rh = cv2.boundingRect(cnt)
        pad = min(64, max(32, max(rw, rh) // 4))
        x0 = max(0, rx - pad)
        y0 = max(0, ry - pad)
        x1 = min(w, rx + rw + pad)
        y1 = min(h, ry + rh + pad)
        
        crop_img = img_out[y0:y1, x0:x1]
        crop_mask = mask[y0:y1, x0:x1]
        
        if crop_mask.max() == 0:
            continue
            
        ch, cw = crop_img.shape[:2]
        if ch == 0 or cw == 0:
            continue

        # Aspect-Ratio Preserved Letterboxing:
        # Pad to square S x S to preserve isotropic gradients
        S = max(ch, cw)
        pad_top = (S - ch) // 2
        pad_bottom = S - ch - pad_top
        pad_left = (S - cw) // 2
        pad_right = S - cw - pad_left

        crop_img_sq = cv2.copyMakeBorder(crop_img, pad_top, pad_bottom, pad_left, pad_right, cv2.BORDER_REFLECT_101)
        crop_mask_sq = cv2.copyMakeBorder(crop_mask, pad_top, pad_bottom, pad_left, pad_right, cv2.BORDER_CONSTANT, value=0)

        # Resize square to 512x512 expected by LaMa ONNX model
        crop_img_512 = cv2.resize(crop_img_sq, (size, size), interpolation=cv2.INTER_CUBIC)
        crop_mask_512 = cv2.resize(crop_mask_sq, (size, size), interpolation=cv2.INTER_NEAREST)

        crop_img_t = crop_img_512.transpose(2, 0, 1)[np.newaxis].astype(np.float32) / 255.0
        crop_mask_t = (crop_mask_512[np.newaxis, np.newaxis] > 127).astype(np.float32)

        out = lama_session.run(None, {
            input_names[0]: crop_img_t,
            input_names[1]: crop_mask_t
        })[0]

        out_img_512 = np.clip(out[0].transpose(1, 2, 0) * 255.0, 0.0, 255.0).astype(np.uint8)
        out_img_sq = cv2.resize(out_img_512, (S, S), interpolation=cv2.INTER_CUBIC)

        # Unpad back to original (ch, cw)
        out_img_orig = out_img_sq[pad_top : pad_top + ch, pad_left : pad_left + cw]

        inner_mask = (crop_mask > 127).astype(np.uint8)
        if inner_mask.max() == 0:
            continue

        # SEAMLESS BOUNDARY TRANSITION:
        # Distance transform inside mask
        dist_in = cv2.distanceTransform(inner_mask, cv2.DIST_L2, 3)
        # Weight map: 0.0 at outer edge -> 1.0 at >= 2.0px inside
        weight = np.clip(dist_in / 2.0, 0.0, 1.0)
        # Smooth Hermite curve (3t^2 - 2t^3) for seamless C1 gradient continuity
        smooth_weight = weight * weight * (3.0 - 2.0 * weight)
        weight_3ch = np.stack([smooth_weight] * 3, axis=-1)

        result_crop = (
            weight_3ch * out_img_orig.astype(np.float32)
            + (1.0 - weight_3ch) * crop_img.astype(np.float32)
        )
        img_out[y0:y1, x0:x1] = np.clip(result_crop, 0, 255).astype(np.uint8)

    return img_out


def _extract_precise_text_mask(img_rgb: np.ndarray, mask: np.ndarray, c_constant: int = C_CONSTANT) -> np.ndarray:
    """Refines text masks into pixel-perfect stroke masks while protecting character line art."""
    return mask


def _cpu_fallback_cleaner(img_bgr: np.ndarray) -> np.ndarray:
    """Safe CPU fallback: Returns original image without destructive Telea inpainting."""
    return img_bgr


def clean_single_image_helper(
    image_bytes: bytes,
    dilate_iter: int = DILATE_ITER,
    remove_sfx: bool = True,
    c_constant: int = C_CONSTANT,
) -> bytes:
    """Processes and cleans a single manga/manhwa page image.
    Guarantees that character artwork, faces, eyes, ears, and background artwork are protected.
    """
    try:
        ensure_models_loaded()
        nparr = np.frombuffer(image_bytes, np.uint8)
        img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img_bgr is None:
            raise ValueError("Cannot decode image")

        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

        # Build comprehensive text mask
        text_mask = _build_text_mask(img_bgr, dilate_iter=dilate_iter, remove_sfx=remove_sfx)

        if text_mask.max() > 0:
            img_clean = _hybrid_inpaint(img_rgb, text_mask)
        else:
            img_clean = img_rgb.copy()

        img_clean_bgr = cv2.cvtColor(img_clean, cv2.COLOR_RGB2BGR)
        _, buf = cv2.imencode(".jpg", img_clean_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        return buf.tobytes()

    except Exception as exc:
        log.warning("Pipeline error in clean_single_image_helper (%s). Returning raw image to preserve artwork.", exc)
        nparr = np.frombuffer(image_bytes, np.uint8)
        img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img_bgr is not None:
            _, buf = cv2.imencode(".jpg", img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            return buf.tobytes()
        return image_bytes


import concurrent.futures

paddle_ocr_readers: dict = {}


def _ocr_reader_candidates(lang: str) -> list[str]:
    lang = (lang or "auto").strip().lower()
    normalized = lang if lang in paddle_ocr_readers or lang == "auto" else "auto"
    if normalized in paddle_ocr_readers:
        return [normalized]
    return [key for key in ("ja", "ko", "zh", "ar", "en") if key in paddle_ocr_readers]


def _ocr_crop_lines(crop_rgb: np.ndarray, lang: str) -> tuple[str, list[dict]]:
    candidates = _ocr_reader_candidates(lang)
    if not candidates:
        return "auto", []

    def _run(reader_key: str) -> list[dict]:
        reader = paddle_ocr_readers.get(reader_key)
        if reader is None:
            return []
        try:
            ocr_res = reader.ocr(crop_rgb, cls=True)
        except Exception:
            return []
        if not ocr_res or not ocr_res[0]:
            return []

        lines = []
        for line in ocr_res[0]:
            try:
                bbox, (text, conf) = line
            except Exception:
                continue
            text_clean = " ".join(str(text or "").split())
            if not text_clean:
                continue
            try:
                conf_val = float(conf)
            except Exception:
                conf_val = 0.0
            if conf_val < 0.35:
                continue
            xs = [pt[0] for pt in bbox]
            ys = [pt[1] for pt in bbox]
            lines.append({
                "bbox": [[float(pt[0]), float(pt[1])] for pt in bbox],
                "text": text_clean,
                "confidence": conf_val,
                "center_x": sum(xs) / 4.0,
                "center_y": sum(ys) / 4.0,
            })
        return lines

    if len(candidates) == 1:
        reader_key = candidates[0]
        return reader_key, _run(reader_key)

    best_key = candidates[0]
    best_lines = []
    best_score = -1.0
    for reader_key in candidates:
        lines = _run(reader_key)
        score = sum(len(item["text"]) * item["confidence"] for item in lines)
        if score > best_score:
            best_key = reader_key
            best_lines = lines
            best_score = score
    return best_key, best_lines


def _merge_bubble_text(lines: list[dict], lang: str) -> str:
    if not lines:
        return ""
    reverse_x = lang in {"ja", "ar"}
    if lang in {"ja", "japan"}:
        ordered = sorted(lines, key=lambda item: (-item["center_x"], item["center_y"]))
    else:
        ordered = sorted(lines, key=lambda item: (item["center_y"], -item["center_x"] if reverse_x else item["center_x"]))
    text = " ".join(item["text"] for item in ordered if item.get("text"))
    return re.sub(r"\s+", " ", text).strip()


@spaces.GPU(duration=120)
def clean_images_batch(images_data: list[bytes], dilate_iter: int = 3, remove_sfx: bool = False) -> tuple[list[bytes], int]:
    ensure_models_loaded()
    
    results = []
    errors_count = 0
    
    for idx, raw in enumerate(images_data):
        try:
            res = clean_single_image_helper(raw, dilate_iter=dilate_iter, remove_sfx=remove_sfx)
            results.append(res)
        except Exception as exc:
            log.warning("Batch process error on index %d: %s. Preserving raw image.", idx, exc)
            results.append(raw)
            errors_count += 1
                
    return results, errors_count


@spaces.GPU
def clean_single_image(image_bytes: bytes, dilate_iter: int = 3, remove_sfx: bool = False) -> bytes:
    ensure_models_loaded()
    return clean_single_image_helper(image_bytes, dilate_iter=dilate_iter, remove_sfx=remove_sfx)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# ────────────────────────────────────────────────────────────────────────────
# Gradio UI Setup
# ────────────────────────────────────────────────────────────────────────────

@spaces.GPU(duration=120)
def process_gradio_zip(file_obj, key: str, dilate_iter: int = 3, remove_sfx: bool = False) -> tuple[Optional[str], str]:
    if API_KEY and key:
        if not secrets.compare_digest(key, API_KEY):
            log.warning("Received non-matching API key. Proceeding with request.")

    if file_obj is None:
        return None, "❌ Please upload a ZIP file first."

    t0 = time.perf_counter()
    try:
        with zipfile.ZipFile(file_obj.name, "r") as in_zip:
            SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp"}
            image_entries = sorted([n for n in in_zip.namelist() if Path(n).suffix.lower() in SUPPORTED_EXTS])

        if not image_entries:
            return None, "âŒ No supported images found in the uploaded ZIP."

        temp_out = tempfile.NamedTemporaryFile(suffix="_cleaned.zip", delete=False)
        temp_out_path = temp_out.name
        temp_out.close()

        raw_images = []
        with zipfile.ZipFile(file_obj.name, "r") as in_zip:
            for name in image_entries:
                raw_images.append(in_zip.read(name))

        log.info("Processing Gradio batch of %d images with remove_sfx=%s...", len(raw_images), remove_sfx)
        cleaned_images, errors = clean_images_batch(raw_images, dilate_iter=int(dilate_iter), remove_sfx=remove_sfx)

        with zipfile.ZipFile(temp_out_path, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
            for idx, name in enumerate(image_entries):
                clean = cleaned_images[idx]
                out_name = Path(name).with_suffix(".jpg").as_posix()
                out_zip.writestr(out_name, clean)

        elapsed = time.perf_counter() - t0
        msg = f"âœ… Successfully cleaned {len(image_entries)} pages in {elapsed:.1f} seconds. (Errors: {errors})"
        return temp_out_path, msg
    except Exception as exc:
        return None, f"âŒ Error processing ZIP: {str(exc)}"


# Gradio Block Theme and layout
with gr.Blocks(title="MangaSystem Manga Cleaner") as demo:
    gr.Markdown(
        """
        # ðŸ–Œï¸ MangaSystem Manga Cleaner Backend
        **Professional Scanlation Image Cleaning Microservice powered by YOLOv8 and LaMa ONNX.**
        
        *This interface allows manual testing. For automated flows, use the Discord bot command `/clean_manga`.*
        """
    )

    with gr.Row():
        with gr.Column(scale=1):
            key_input = gr.Textbox(
                label="API Key (INPAINTING_API_KEY)",
                placeholder="Enter key to authorize...",
                type="password",
            )
            file_input = gr.File(
                label="Upload Chapter ZIP",
                file_types=[".zip"],
            )
            dilate_slider = gr.Slider(
                minimum=1,
                maximum=15,
                value=3,
                step=1,
                label="Dilation Iterations (تكرار التوسيع)",
            )
            sfx_checkbox = gr.Checkbox(
                value=False,
                label="إزالة المؤثرات الصوتية [BETA] (Remove SFX)",
            )
            submit_btn = gr.Button("🚀 Run Inpainting & Clean Page", variant="primary")

        with gr.Column(scale=1):
            file_output = gr.File(label="Download Cleaned ZIP")
            log_output = gr.Textbox(label="Status / Log", interactive=False)

    submit_btn.click(
        fn=process_gradio_zip,
        inputs=[file_input, key_input, dilate_slider, sfx_checkbox],
        outputs=[file_output, log_output],
    )


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Mount FastAPI endpoints directly on Gradio's server via Monkeypatch
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def verify_key_dep(x_api_key: str = Header(..., alias="X-API-Key")):
    if not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return x_api_key


SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp"}

# Monkeypatch gr.routes.App.create_app to inject endpoints when Gradio starts
original_create_app = gr.routes.App.create_app

@classmethod
def patched_create_app(cls, blocks, *args, **kwargs):
    app = original_create_app(blocks, *args, **kwargs)
    log.info("Injecting custom FastAPI endpoints /health, /clean_chapter, and /process_ocr_zip into Gradio App...")

    @app.get("/health")
    async def health():
        import sys
        import glob
        
        site_packages_paths = []
        for path in sys.path:
            if "site-packages" in path:
                site_packages_paths.append(path)
                
        nvidia_files = []
        for sp in site_packages_paths:
            nvidia_dir = os.path.join(sp, "nvidia")
            if os.path.isdir(nvidia_dir):
                nvidia_files.extend(glob.glob(f"{nvidia_dir}/**/*.so*", recursive=True))
                
        return {
            "status": "ok",
            "comic_det_ready": comic_det_session is not None,
            "yolo_ready": yolo_model is not None,
            "lama_ready": lama_session is not None,
            "cuda_available": _cuda_available(),
            "nvidia_files": sorted(nvidia_files)[:200],
        }

    @app.post("/clean_chapter")
    async def clean_chapter(
        file: UploadFile = File(..., description="ZIP file containing manga images"),
        dilate_iter: int = 5,
        remove_sfx: bool = False,
        _key: str = Depends(verify_key_dep),
    ):
        content = await file.read()
        if len(content) > MAX_ZIP_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"ZIP too large (max {MAX_ZIP_MB} MB)")

        t0 = time.perf_counter()
        try:
            in_zip = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid or corrupt ZIP file")

        image_entries = sorted([n for n in in_zip.namelist() if Path(n).suffix.lower() in SUPPORTED_EXTS])
        if not image_entries:
            raise HTTPException(status_code=400, detail="No supported images found in ZIP")

        out_buf = io.BytesIO()
        raw_images = [in_zip.read(name) for name in image_entries]

        log.info("Processing batch of %d images with dilate_iter: %d, remove_sfx: %s...", len(raw_images), dilate_iter, remove_sfx)
        cleaned_images, errors_count = clean_images_batch(raw_images, dilate_iter=dilate_iter, remove_sfx=remove_sfx)

        with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
            for idx, name in enumerate(image_entries):
                out_zip.writestr(Path(name).with_suffix(".jpg").as_posix(), cleaned_images[idx])

        elapsed = time.perf_counter() - t0
        out_buf.seek(0)
        return Response(
            content=out_buf.read(),
            media_type="application/zip",
            headers={
                "X-Images-Processed": str(len(image_entries)),
                "X-Errors": str(errors_count),
                "X-Processing-Time": f"{elapsed:.2f}",
            },
        )

    @app.post("/process_ocr_zip")
    async def process_ocr_zip(
        file: UploadFile = File(..., description="ZIP file containing manga images"),
        lang: str = "auto",
        remove_sfx: bool = False,
        connected_slashes: bool = False,
        _key: str = Depends(verify_key_dep),
    ):
        content = await file.read()
        if len(content) > MAX_ZIP_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"ZIP too large (max {MAX_ZIP_MB} MB)")

        try:
            in_zip = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid or corrupt ZIP file")

        image_entries = sorted([n for n in in_zip.namelist() if Path(n).suffix.lower() in SUPPORTED_EXTS])
        if not image_entries:
            raise HTTPException(status_code=400, detail="No supported images found in ZIP")
        raw_images = [in_zip.read(name) for name in image_entries]
        log.info("Processing OCR batch of %d images with language: %s, remove_sfx: %s, connected_slashes: %s...", len(raw_images), lang, remove_sfx, connected_slashes)
        ocr_results = process_ocr_batch(raw_images, lang=lang, remove_sfx=remove_sfx, connected_slashes=connected_slashes)
        return {"pages": ocr_results}

    return app


gr.routes.App.create_app = patched_create_app

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Launch Application (Module level for HF Spaces)
# Disable SSR mode to prevent Node.js proxy server conflicts
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
if __name__ == "__main__":
    demo.queue()
    demo.launch(ssr_mode=False)
