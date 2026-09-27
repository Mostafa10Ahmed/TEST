<div align="center">

# 🎨 INPAINTING-CORE | AI Manga Cleaner & Erase Microservice

[![PyTorch](https://img.shields.io/badge/PyTorch-GPU%20Accelerated-EE4C2C?style=for-the-badge&logo=pytorch)](https://pytorch.org/)
[![HuggingFace ZeroGPU](https://img.shields.io/badge/HuggingFace-ZeroGPU%20Enabled-FFD21E?style=for-the-badge&logo=huggingface)](https://huggingface.co/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Gradio%20Unified-009688?style=for-the-badge&logo=fastapi)](https://fastapi.tiangolo.com/)

**خادم الذكاء الاصطناعي الفائق لإزالة نصوص وتبييض المانجا بـ LaMa Model**

---

</div>

## 🌟 الميزات الفائقة

- **🧠 LaMa Large Mask Inpainting**: استعادة خلفيات المانجا والرسومات بدقة متناهية بدون ترك أثر للنص.
- **🎯 Hard Mask Blending**: خوارزمية مخصصة تجنب حدوث التشويه الشفاف (Ghost Text).
- **🚀 HuggingFace ZeroGPU & RTX 6000**: تحسينات استهلاك الذاكرة وتخصيص تسريع الـ GPU الديناميكي.
- **🔌 Unified API**: خادم يدمج بين FastAPI للربط المباشر مع البوت، وواجهة Gradio للتجربة المباشرة من المتصفح.

---

## ⚡ التشغيل والاستخدام

```bash
# تثبيت التبعات وتشغيل الخادم
pip install -r requirements.txt
python app.py
```

- **رابط الواجهة التجريبية**: `http://localhost:7860`
- **نقطة نهاية الـ API**: `POST /api/clean`
