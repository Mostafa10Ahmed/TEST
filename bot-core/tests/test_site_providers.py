import unittest
from providers.manager import ProviderManager

class TestSiteProviders(unittest.TestCase):

    def setUp(self):
        self.manager = ProviderManager()

    def test_provider_routing(self):
        test_urls = [
            "https://comix.to/title/dkd3-overlord-of-insects",
            "https://flamecomics.xyz/series/166",
            "https://www.mgeko.cc/manga/imprisoned-one-million-years",
            "https://www.kuaikanmanhua.com/web/topic/4319/",
            "https://ridibooks.com/books/6305000001",
            "https://page.kakao.com/content/69949760/",
            "https://ac.qq.com/",
            "https://manga.bilibili.com/",
            "https://www.iqiyi.com/manhua/",
            "https://comic.naver.com/",
            "https://www.lezhin.com/",
            "https://toptoon.com/",
            "https://piccoma.com/",
            "https://www.comico.jp/",
            "https://jumptoon.com/",
            "https://www.mrblue.com/",
            "https://manta.net/",
            "https://www.bomtoon.com/",
            "https://www.manga-up.com/",
        ]

        for url in test_urls:
            provider = self.manager.get_provider(url)
            self.assertIsNotNone(provider, f"Failed to resolve provider for URL: {url}")
            print(f"[OK] URL: {url:55s} -> Provider: {provider.__class__.__name__}")

if __name__ == "__main__":
    unittest.main()
