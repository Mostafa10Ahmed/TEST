import unittest
import os

from providers.shinigami_provider import ShinigamiProvider


class TestShinigamiProvider(unittest.TestCase):
    def setUp(self):
        self.provider = ShinigamiProvider()

    def test_worker_fallback_chapters(self):
        class Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "ok": True,
                    "chapters": {
                        "62.0": "https://g.shinigami.asia/chapter/11111111-1111-1111-1111-111111111111",
                        "61.0": "https://g.shinigami.asia/chapter/22222222-2222-2222-2222-222222222222",
                    },
                }

        import providers.shinigami_provider as mod
        old_post = mod.requests.post
        try:
            mod.requests.post = lambda *a, **k: Resp()  # type: ignore
            os.environ["HF_WORKER_URL"] = "https://worker.example.com"
            os.environ["HF_WORKER_KEY"] = "secret"
            out = self.provider._chapters_from_worker("https://g.shinigami.asia/series/x")
            self.assertEqual(len(out), 2)
            self.assertIn(62.0, out)
        finally:
            mod.requests.post = old_post  # type: ignore
            os.environ.pop("HF_WORKER_URL", None)
            os.environ.pop("HF_WORKER_KEY", None)

    def test_worker_fallback_images(self):
        class Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "ok": True,
                    "images": [
                        "https://cdn.example.com/1.jpg",
                        "https://cdn.example.com/2.webp",
                    ],
                }

        import providers.shinigami_provider as mod
        old_post = mod.requests.post
        try:
            mod.requests.post = lambda *a, **k: Resp()  # type: ignore
            os.environ["HF_WORKER_URL"] = "https://worker.example.com"
            os.environ["HF_WORKER_KEY"] = "secret"
            out = self.provider._images_from_worker("https://g.shinigami.asia/chapter/x")
            self.assertEqual(len(out), 2)
        finally:
            mod.requests.post = old_post  # type: ignore
            os.environ.pop("HF_WORKER_URL", None)
            os.environ.pop("HF_WORKER_KEY", None)

    def test_shngm_api_chapters(self):
        class Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "retcode": 0,
                    "data": [
                        {"chapter_id": "11111111-1111-1111-1111-111111111111", "chapter_number": 62},
                        {"chapter_id": "22222222-2222-2222-2222-222222222222", "chapter_number": "61"},
                    ],
                }

        import providers.shinigami_provider as mod
        old_get = mod.requests.get
        try:
            mod.requests.get = lambda *a, **k: Resp()  # type: ignore
            out = self.provider._chapters_from_shngm_api("5d56add0-399a-47f6-ba93-12af1d53accf")
            self.assertEqual(len(out), 2)
            self.assertEqual(out[62.0], "https://g.shinigami.asia/chapter/11111111-1111-1111-1111-111111111111")
        finally:
            mod.requests.get = old_get  # type: ignore

    def test_shngm_api_images(self):
        class Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "retcode": 0,
                    "data": {
                        "base_url": "https://assets.shngm.id",
                        "chapter": {
                            "path": "/chapter/manga_x/chapter_y/",
                            "data": ["00.jpg", "01.webp"],
                        },
                    },
                }

        import providers.shinigami_provider as mod
        old_get = mod.requests.get
        try:
            mod.requests.get = lambda *a, **k: Resp()  # type: ignore
            out = self.provider._images_from_shngm_api("11111111-1111-1111-1111-111111111111")
            self.assertEqual(out, [
                "https://assets.shngm.id/chapter/manga_x/chapter_y/00.jpg",
                "https://assets.shngm.id/chapter/manga_x/chapter_y/01.webp",
            ])
        finally:
            mod.requests.get = old_get  # type: ignore

    def test_extracts_chapters_from_nested_next_data(self):
        html = """
        <html><head></head><body>
        <script id="__NEXT_DATA__" type="application/json">
        {
          "props": {
            "pageProps": {
              "series": {
                "chapters": [
                  {"id":"11111111-1111-1111-1111-111111111111","chapterNumber":62},
                  {"meta":{"id":"22222222-2222-2222-2222-222222222222","number":"61"}},
                  {"node":{"href":"/chapter/33333333-3333-3333-3333-333333333333","title":"Chapter 60"}}
                ]
              }
            }
          }
        }
        </script>
        </body></html>
        """
        chapters = self.provider._chapters_from_next_data(html)
        self.assertEqual(len(chapters), 3)
        self.assertIn(62.0, chapters)
        self.assertIn(61.0, chapters)
        self.assertIn(60.0, chapters)

    def test_extracts_images_from_next_data_strings(self):
        html = """
        <html><body>
        <script id="__NEXT_DATA__" type="application/json">
        {
          "props": {
            "pageProps": {
              "chapter": {
                "pages": [
                  {"url":"https://cdn.example.com/manga/1.webp"},
                  {"src":"//cdn.example.com/manga/2.jpg"},
                  {"img":"/images/3.png"},
                  {"logo":"https://cdn.example.com/logo.png"}
                ]
              }
            }
          }
        }
        </script>
        </body></html>
        """

        self.provider._fetch = lambda _: html  # type: ignore
        images = self.provider._sync_get_images(
            "https://g.shinigami.asia/chapter/11111111-1111-1111-1111-111111111111"
        )
        self.assertIn("https://cdn.example.com/manga/1.webp", images)
        self.assertIn("https://cdn.example.com/manga/2.jpg", images)
        self.assertIn("https://g.shinigami.asia/images/3.png", images)
        self.assertNotIn("https://cdn.example.com/logo.png", images)


if __name__ == "__main__":
    unittest.main()
