import io
import os
import math
from typing import List, Optional, Tuple, Union
from PIL import Image, ImageStat

class ImageFilter:
    """
    مُصفّي الصور الذكي المخصص لصفحات المانوا/المانوا.
    يقوم بكشف واستبعاد الإعلانات، الصور المتشابهة/المكررة (pHash)، الصور التالفة، وأغلفة الحقوق.
    """

    def __init__(self, known_ad_hashes: Optional[List[str]] = None, min_width: int = 120, min_height: int = 120, min_bytes: int = 4000):
        self.known_ad_hashes = known_ad_hashes or []
        self.min_width = min_width
        self.min_height = min_height
        self.min_bytes = min_bytes

    @staticmethod
    def compute_phash(img: Image.Image, hash_size: int = 8) -> str:
        """يحسب البصمة الرقمية للصورة (Perceptual Hash)"""
        try:
            gray = img.convert('L').resize((hash_size, hash_size), Image.Resampling.LANCZOS)
            if hasattr(gray, "get_flattened_data"):
                pixels = list(gray.get_flattened_data())
            else:
                pixels = list(gray.getdata())
            avg = sum(pixels) / len(pixels)
            bits = "".join("1" if p > avg else "0" for p in pixels)
            return hex(int(bits, 2))[2:].zfill((hash_size * hash_size) // 4)
        except Exception:
            return ""

    @staticmethod
    def hamming_distance(hash1: str, hash2: str) -> int:
        """يحسب مسافة الهامينغ بين بصمتين لتحديد مدى التشابه بين الصور"""
        if not hash1 or not hash2 or len(hash1) != len(hash2):
            return 999
        try:
            val1 = int(hash1, 16)
            val2 = int(hash2, 16)
            return bin(val1 ^ val2).count('1')
        except Exception:
            return 999

    def is_blank_or_solid_color(self, img: Image.Image, threshold: float = 2.5) -> bool:
        """يتحقق هل الصورة لون واحد ثابت (صفحة فارغة أو تالفة)"""
        try:
            gray = img.convert('L')
            stat = ImageStat.Stat(gray)
            stddev = stat.stddev[0]
            return stddev < threshold
        except Exception:
            return False

    def is_valid_manga_page(
        self, 
        image_input: Union[bytes, str, Image.Image], 
        ad_hash_threshold: int = 6
    ) -> Tuple[bool, str]:
        """
        يفحص الصورة ويحدد هل هي صفحة مانوا سليمة أم يجب إستبعادها.
        """
        raw_bytes = None
        img = None

        try:
            if isinstance(image_input, bytes):
                raw_bytes = image_input
                if len(raw_bytes) < self.min_bytes:
                    return False, f"حجم الملف صغير جداً ({len(raw_bytes)} B < {self.min_bytes} B)"
                img = Image.open(io.BytesIO(raw_bytes))
            elif isinstance(image_input, str):
                if os.path.exists(image_input):
                    with open(image_input, "rb") as f:
                        raw_bytes = f.read()
                    if len(raw_bytes) < self.min_bytes:
                        return False, f"حجم الملف صغير جداً ({len(raw_bytes)} B)"
                    img = Image.open(image_input)
                else:
                    return False, "الملف غير موجود"
            elif isinstance(image_input, Image.Image):
                img = image_input
            else:
                return False, "نوع المدخلات غير معروف"

            # 1. الأبعاد
            width, height = img.size
            if width < self.min_width or height < self.min_height:
                return False, f"أبعاد صغيرة ({width}x{height})"

            # 2. نسبة العرض إلى الارتفاع (إعلانات شريطية أفية مفرطة)
            aspect_ratio = width / float(height)
            if aspect_ratio > 4.0:
                return False, f"شريط إعلاني أفقي ({aspect_ratio:.2f})"

            # 3. صور فارغة / أحادية اللون
            if self.is_blank_or_solid_color(img):
                return False, "صورة فارغة أو بلون أحادي ثابت"

            # 4. مقارنة بصمات pHash للإعلانات والشعارات
            if self.known_ad_hashes:
                img_hash = self.compute_phash(img)
                for known_hash in self.known_ad_hashes:
                    dist = self.hamming_distance(img_hash, known_hash)
                    if dist <= ad_hash_threshold:
                        return False, f"إعلان أو شعار حقوق مكرر (pHash dist={dist})"

            return True, "OK"

        except Exception as e:
            return False, f"خطأ في معالجة الصورة: {e}"

default_image_filter = ImageFilter()
