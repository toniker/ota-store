"""Tests for finding an app's icon."""

from __future__ import annotations

import json
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from ota_store import icons
from ota_store.config import App
from tests.helpers import make_ipa, png


def cgbi(width: int, height: int) -> bytes:
    """A PNG with Apple's CgBI chunk in front, as Xcode writes for some bundle images."""
    data = b"\x50\x00\x20\x06"
    chunk = struct.pack(">I", len(data)) + b"CgBI" + data + struct.pack(">I", zlib.crc32(b"CgBI" + data))
    normal = png(width, height)
    return normal[:8] + chunk + normal[8:]


def size_of(path: Path):
    return icons.png_size(path.read_bytes())[:2]


class IconTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.dir = self.root / "src"
        self.dir.mkdir()
        self.dest = self.root / "out" / "icon.png"
        self.ipa = make_ipa(self.root / "App.ipa")

    def app(self, icon=None, scheme="App") -> App:
        return App(id="app", name="App", dir=self.dir, scheme=scheme,
                   icon=self.dir / icon if icon else None)

    def icon_set(self, relative: str, images) -> Path:
        """An .appiconset at `relative` with `images`: (filename, width, entry fields)."""
        folder = self.dir / relative
        folder.mkdir(parents=True)
        entries = []
        for filename, width, fields in images:
            (folder / filename).write_bytes(png(width, width))
            entries.append(dict(fields, filename=filename))
        (folder / "Contents.json").write_text(json.dumps({"images": entries}))
        return folder

    def test_png(self):
        (self.dir / "icon.png").write_bytes(png(40, 40))
        self.assertTrue(icons.extract_icon(self.app("icon.png"), self.ipa, self.dest))
        self.assertEqual(size_of(self.dest), (40, 40))

    def test_large_png_is_shrunk(self):
        (self.dir / "icon.png").write_bytes(png(600, 600))
        self.assertTrue(icons.extract_icon(self.app("icon.png"), self.ipa, self.dest))
        self.assertEqual(size_of(self.dest), (256, 256))

    def test_icon_set_prefers_largest_default(self):
        self.icon_set("Assets.xcassets/AppIcon.appiconset", [
            ("small.png", 20, {"idiom": "iphone", "size": "20x20", "scale": "1x"}),
            ("dark.png", 90, {"idiom": "universal", "platform": "ios",
                              "appearances": [{"appearance": "luminosity", "value": "dark"}]}),
            ("mac.png", 120, {"idiom": "mac", "size": "512x512"}),
            ("watch.png", 110, {"idiom": "universal", "platform": "watchos"}),
            ("light.png", 60, {"idiom": "universal", "platform": "ios"}),
        ])
        app = self.app("Assets.xcassets/AppIcon.appiconset")
        self.assertTrue(icons.extract_icon(app, self.ipa, self.dest))
        self.assertEqual(size_of(self.dest), (60, 60))

    def test_icon_set_falls_back_to_dark(self):
        self.icon_set("AppIcon.appiconset", [
            ("dark.png", 30, {"idiom": "universal", "appearances": [{"appearance": "luminosity", "value": "dark"}]}),
            ("tinted.png", 50, {"idiom": "universal", "appearances": [{"appearance": "luminosity", "value": "tinted"}]}),
        ])
        self.assertTrue(icons.extract_icon(self.app("AppIcon.appiconset"), self.ipa, self.dest))
        self.assertEqual(size_of(self.dest), (30, 30))

    def test_search_finds_icon_set(self):
        self.icon_set("App/Assets.xcassets/AppIcon.appiconset", [("a.png", 33, {"idiom": "universal"})])
        self.assertTrue(icons.extract_icon(self.app(), self.ipa, self.dest))
        self.assertEqual(size_of(self.dest), (33, 33))

    def test_search_skips_caches_and_hidden_folders(self):
        for skipped in ("Pods/X", "build/Y", ".hidden/Z", "DerivedData/W", "node_modules/V"):
            self.icon_set(f"{skipped}/AppIcon.appiconset", [("a.png", 10, {"idiom": "universal"})])
        self.assertEqual(icons.search(self.app()), [])

    def test_search_prefers_app_target(self):
        self.icon_set("AppWidgetsExtension/Assets.xcassets/AppIcon.appiconset", [("w.png", 11, {})])
        self.icon_set("AAATests/Assets.xcassets/AppIcon.appiconset", [("t.png", 12, {})])
        self.icon_set("Sources/Assets.xcassets/AppIcon.appiconset", [("s.png", 13, {})])
        self.icon_set("App/Assets.xcassets/AppIcon.appiconset", [("a.png", 14, {})])
        found = [path.relative_to(self.dir).parts[0] for path in icons.search(self.app())]
        self.assertEqual(found, ["App", "Sources", "AAATests", "AppWidgetsExtension"])

    def test_search_puts_icon_composer_first(self):
        self.icon_set("App/Assets.xcassets/AppIcon.appiconset", [("a.png", 14, {})])
        (self.dir / "Other" / "AppIcon.icon").mkdir(parents=True)
        self.assertEqual(icons.search(self.app())[0].name, "AppIcon.icon")

    def test_empty_icon_set_falls_through_to_ipa(self):
        # Like an app whose AppIcon.appiconset has a Contents.json and no images.
        self.icon_set("App/Assets.xcassets/AppIcon.appiconset", [])
        ipa = make_ipa(self.root / "Icon.ipa", icon=png(120, 120))
        self.assertTrue(icons.extract_icon(self.app(), ipa, self.dest))
        self.assertEqual(size_of(self.dest), (120, 120))

    def test_ipa_without_app(self):
        ipa = make_ipa(self.root / "Icon.ipa", icon=png(87, 87))
        self.assertTrue(icons.extract_icon(None, ipa, self.dest))
        self.assertEqual(size_of(self.dest), (87, 87))

    def test_ipa_takes_largest_readable(self):
        ipa = self.root / "Icons.ipa"
        make_ipa(ipa, icon=png(120, 120))
        with zipfile.ZipFile(ipa, "a") as archive:
            archive.writestr("Payload/Fake.app/AppIcon60x60@3x.png", png(180, 180))
            archive.writestr("Payload/Fake.app/Other.png", png(500, 500))
        self.assertTrue(icons.extract_icon(None, ipa, self.dest))
        self.assertEqual(size_of(self.dest), (180, 180))

    def test_cgbi_is_skipped(self):
        self.assertTrue(icons.png_size(cgbi(5, 5))[2])
        ipa = make_ipa(self.root / "Crushed.ipa", icon=cgbi(120, 120))
        self.assertFalse(icons.extract_icon(None, ipa, self.dest))
        self.assertFalse(self.dest.exists())

    def test_cgbi_beside_normal(self):
        ipa = self.root / "Mixed.ipa"
        make_ipa(ipa, icon=png(120, 120))
        with zipfile.ZipFile(ipa, "a") as archive:
            archive.writestr("Payload/Fake.app/AppIcon60x60@3x.png", cgbi(180, 180))
        self.assertTrue(icons.extract_icon(None, ipa, self.dest))
        self.assertEqual(size_of(self.dest), (120, 120))

    def test_nothing_found(self):
        (self.dir / "Sources").mkdir()
        self.assertFalse(icons.extract_icon(self.app("missing.png"), self.ipa, self.dest))
        self.assertFalse(self.dest.exists())

    def test_not_an_ipa(self):
        broken = self.root / "broken.ipa"
        broken.write_bytes(b"not a zip")
        self.assertFalse(icons.extract_icon(None, broken, self.dest))
        self.assertFalse(icons.extract_icon(None, self.root / "missing.ipa", self.dest))

    def test_not_a_png(self):
        (self.dir / "icon.png").write_bytes(b"GIF89a")
        self.assertFalse(icons.extract_icon(App(id="a", name="a", icon=self.dir / "icon.png"), self.ipa, self.dest))


if __name__ == "__main__":
    unittest.main()
