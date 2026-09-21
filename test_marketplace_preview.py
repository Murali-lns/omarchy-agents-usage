"""Checks for the marketplace preview contract and README rendering."""

from pathlib import Path
import struct
import unittest


ROOT = Path(__file__).parent
PREVIEW_PATH = ROOT / "preview.png"
README_PATH = ROOT / "README.md"


class TestMarketplacePreview(unittest.TestCase):
    def test_root_preview_is_supported_and_within_marketplace_limits(self):
        data = PREVIEW_PATH.read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(data[12:16], b"IHDR")
        width, height = struct.unpack(">II", data[16:24])
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        self.assertLessEqual(width * height, 40_000_000)
        self.assertLessEqual(len(data), 50 * 1024 * 1024)

    def test_readme_uses_the_marketplace_preview(self):
        readme = README_PATH.read_text(encoding="utf-8")
        self.assertIn("](preview.png)", readme)


if __name__ == "__main__":
    unittest.main()
