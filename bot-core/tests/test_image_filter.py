import io
import unittest
from PIL import Image, ImageDraw
from image_filter import ImageFilter, default_image_filter

class TestImageFilter(unittest.TestCase):

    def setUp(self):
        self.filter = ImageFilter(min_width=100, min_height=100, min_bytes=100)

    def test_valid_manga_page(self):
        # 400x600 image with gradient pattern
        img = Image.new("RGB", (400, 600), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle([50, 50, 350, 550], fill=(0, 0, 0))
        draw.line([0, 0, 400, 600], fill=(200, 100, 50), width=5)

        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        valid, reason = self.filter.is_valid_manga_page(raw_bytes)
        self.assertTrue(valid, f"Expected valid page, got: {reason}")

    def test_tiny_image(self):
        img = Image.new("RGB", (50, 50), color=(200, 200, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        valid, reason = self.filter.is_valid_manga_page(raw_bytes)
        self.assertFalse(valid)
        self.assertIn("أبعاد صغيرة", reason)

    def test_wide_banner_ad(self):
        img = Image.new("RGB", (900, 100), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        valid, reason = self.filter.is_valid_manga_page(raw_bytes)
        self.assertFalse(valid)
        self.assertIn("شريط إعلاني", reason)

    def test_blank_solid_color(self):
        img = Image.new("RGB", (300, 500), color=(128, 128, 128))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        valid, reason = self.filter.is_valid_manga_page(raw_bytes)
        self.assertFalse(valid)
        self.assertIn("لون أحادي", reason)

    def test_phash_ad_detection(self):
        # Create an ad image
        ad_img = Image.new("RGB", (300, 400), color=(255, 255, 0))
        draw = ImageDraw.Draw(ad_img)
        draw.text((10, 10), "PROMO ADVERT", fill=(0, 0, 0))
        
        ad_hash = ImageFilter.compute_phash(ad_img)
        custom_filter = ImageFilter(known_ad_hashes=[ad_hash], min_bytes=50)

        buf = io.BytesIO()
        ad_img.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        valid, reason = custom_filter.is_valid_manga_page(raw_bytes)
        self.assertFalse(valid)
        self.assertIn("إعلان أو شعار حقوق مكرر", reason)

if __name__ == "__main__":
    unittest.main()
