"""Validate the renamed static site and deployment contracts locally."""
from html.parser import HTMLParser
from pathlib import Path
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.canonical = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        for name in ("href", "src"):
            if attrs.get(name):
                self.links.append(attrs[name])
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical.append(attrs.get("href"))


class SiteTests(unittest.TestCase):
    def test_brand_canonical_links_and_local_assets(self):
        site = ROOT / "site"
        for page in site.glob("*.html"):
            with self.subTest(page=page.name):
                text = page.read_text()
                self.assertIn("Mesh", text)
                self.assertNotIn("daochi.net", text)
                self.assertNotIn("kryonlabs/daochi", text)
                parsed = Links()
                parsed.feed(text)
                self.assertEqual(len(parsed.canonical), 1)
                self.assertEqual(urlsplit(parsed.canonical[0]).netloc, "mesh.ziran-lang.org")
                for link in parsed.links:
                    url = urlsplit(link)
                    if url.netloc or url.scheme or not url.path:
                        continue
                    target = site / url.path.lstrip("/") if url.path.startswith("/") else page.parent / url.path
                    if target.is_dir():
                        target /= "index.html"
                    self.assertTrue(target.is_file(), f"{page.name}: missing {link}")

    def test_physical_bucket_and_api_route_remain_stable(self):
        config = (ROOT / "cdn/wrangler.jsonc").read_text()
        self.assertIn('"name": "mesh-packages"', config)
        self.assertIn('"bucket_name": "daochi-packages"', config)
        self.assertIn("api.waozi.xyz/api/v2/packages/*", config)


if __name__ == "__main__":
    unittest.main()
