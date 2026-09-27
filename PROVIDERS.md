# 🌐 دليل محركات جلب المواقع (Manga Providers)

يحتوي مجلد `bot-core/providers/` على المحركات الخاصة باستخراج الفصول والعناوين وقوائم الصور من منصات المانجا والمانهوا المختلفة.

---

## 🏛️ البنية الهندسية لـ Provider

تعتمد جميع المحركات على المفهوم البرمجي الموحد المعرف في `base_provider.py`:

```text
               ┌───────────────────────┐
               │   BaseMangaProvider   │
               └───────────┬───────────┘
                           │
    ┌──────────────────────┼──────────────────────┐
    │                      │                      │
┌───┴──────────┐   ┌───────┴──────┐   ┌───────────┴──────────┐
│ AsuraProvider│   │ ComixProvider│   │  PaginatedScraper    │
└──────────────┘   └──────────────┘   └──────────────────────┘
```

---

## 🛠️ نمط التعامل مع برمجيات المواقع

1. **مواقع WordPress / Madara Plugin**:
   - يتم التعامل معها عبر `madara_provider.py` باستخدام طلبات AJAX لجلب قائمة الفصول وكتل الصور المباشرة.
2. **مواقع SPA / React / Next.js / Nuxt.js**:
   - يتم التعامل معها عبر تحليل بيانات الـ Hydration (مثل `__NEXT_DATA__` أو `__NUXT__`) وقراءة JSON المدمج بالصفحة.
3. **المواقع المحمية بـ Cloudflare**:
   - يتم التعامل معها عبر مكتبة `scrapling` المتقدمة أو ربط محرك `flaresolverr` لفك تشفير حماية الـ JavaScript Nonce والتحديات الأمنية.

---

## 🔒 نظام كشف الأقفال والرموز (Lock Detector)

المسؤول عن فحص ما إذا كان الفصل مجانياً أو مدفوعاً/مغلقاً برمز:
- الكود في `lock_detector.py`.
- يقوم بتحليل عناصر الصفحة (شارات القفل، كلمات السر، كود الاستجابة) لتجنب تنزيل صفحات فارغة أو شاشات تسجيل الدخول.

---

## ➕ كيفية إضافة موقع جديد

### الخطوة 1: إنشاء ملف المحرك الجديد
أنشئ ملفاً جديداً داخل `bot-core/providers/` باسم الموقع (مثال: `mymanga_provider.py`).

### الخطوة 2: وراثة الكلاس `BaseMangaProvider`
```python
from typing import List, Dict, Any
from .base_provider import BaseMangaProvider

class MyMangaProvider(BaseMangaProvider):
    site_name = "MyManga"
    allowed_domains = ["mymanga.com", "www.mymanga.com"]
    
    async def get_series_info(self, url: str) -> Dict[str, Any]:
        """جلب تفاصيل المانجا وقائمة الفصول"""
        html = await self.fetch_html(url)
        title = self.soup_select_text(html, "h1.entry-title")
        chapters = []
        # استخراج الفصول...
        return {
            "title": title,
            "chapters": chapters
        }

    async def get_chapter_images(self, chapter_url: str) -> List[str]:
        """جلب روابط صور الفصل المباشرة"""
        html = await self.fetch_html(chapter_url)
        images = self.extract_image_urls(html, ".reader-area img")
        return images
```

### الخطوة 3: تسجيل المحرك في `manager.py`
افتح `bot-core/providers/manager.py` وأضف الموديل الجديد إلى قائمة المحركات المسجلة:

```python
from .mymanga_provider import MyMangaProvider

self.register_provider(MyMangaProvider())
```
