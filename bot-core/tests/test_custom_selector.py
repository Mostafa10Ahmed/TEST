import unittest
try:
    from providers.custom_selector_scraper import parse_latest_from_html, CustomSelectorRule
except ImportError:
    from mangasystem_bot_core.providers.custom_selector_scraper import parse_latest_from_html, CustomSelectorRule


class TestCustomSelector(unittest.TestCase):
    def test_json_selector_scraping(self):
        html = """
        <html>
            <body>
                <div class="chapter-item">
                    <a class="chapter-link" href="/series/one-piece/chapter-1090">Chapter 1090</a>
                </div>
                <div class="chapter-item">
                    <a class="chapter-link" href="/series/one-piece/chapter-1089">Chapter 1089</a>
                </div>
            </body>
        </html>
        """
        
        # Test custom JSON-based selector config
        raw_config = '{"item": ".chapter-item", "link": ".chapter-link", "title": ".chapter-link", "number_regex": "chapter\\\\s+(\\\\d+)", "get_first": true}'
        rule = CustomSelectorRule(
            domain="example.com",
            selector=".chapter-item",
            url_attr="href",
            number_regex="",
            get_first=True,
            use_browser=False,
            notes="",
            raw_config=raw_config
        )
        
        result = parse_latest_from_html(html, "https://example.com/series/one-piece", rule)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 1090.0)
        self.assertEqual(result[1], "https://example.com/series/one-piece/chapter-1090")

if __name__ == "__main__":
    unittest.main()
