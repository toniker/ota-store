import json
import os
import unittest
from pathlib import Path

from ota_store import config
from tests.helpers import temp_store


class ConfigTests(unittest.TestCase):
    def test_missing_file_gives_defaults(self):
        with temp_store():
            loaded = config.load_config()
            self.assertEqual(loaded.apps, {})
            self.assertEqual(loaded.port, config.DEFAULT_PORT)

    def test_round_trip_resolves_and_contracts_paths(self):
        with temp_store() as root:
            project_dir = root / "src" / "TrailMap"
            entry = {
                "id": "trailmap", "name": "TrailMap", "dir": str(project_dir),
                "project": "TrailMap.xcodeproj", "scheme": "TrailMap",
                "inputs": ["../Shared"], "somethingNew": 1,
            }
            config.config_path().parent.mkdir(parents=True)
            config.config_path().write_text(json.dumps({"title": "Mine", "apps": [entry]}))

            loaded = config.load_config(apply_environment=False)
            app = loaded.app("trailmap")
            self.assertEqual(loaded.title, "Mine")
            self.assertEqual(app.project, project_dir / "TrailMap.xcodeproj")
            self.assertEqual(app.inputs, [root / "src" / "Shared"])
            self.assertTrue(app.buildable)
            self.assertEqual(app.extra, {"somethingNew": 1})

            config.save_config(loaded)
            saved = json.loads(config.config_path().read_text())
            self.assertEqual(saved["title"], "Mine")
            self.assertEqual(saved["apps"][0]["project"], "TrailMap.xcodeproj")
            self.assertEqual(saved["apps"][0]["somethingNew"], 1)
            self.assertEqual(config.load_config(apply_environment=False).app("trailmap"), app)

    def test_environment_overrides(self):
        with temp_store():
            os.environ["OTA_STORE_PORT"] = "9999"
            os.environ["OTA_STORE_URL"] = "https://apps.example.ts.net/"
            try:
                loaded = config.load_config()
            finally:
                del os.environ["OTA_STORE_PORT"], os.environ["OTA_STORE_URL"]
            self.assertEqual(loaded.port, 9999)
            self.assertEqual(loaded.url, "https://apps.example.ts.net")

    def test_rejects_bad_ids_and_duplicates(self):
        with temp_store():
            config.config_path().parent.mkdir(parents=True)
            for apps in ([{"id": "Not OK"}], [{"id": "a"}, {"id": "a"}]):
                config.config_path().write_text(json.dumps({"apps": apps}))
                with self.assertRaises(config.ConfigError):
                    config.load_config()

    def test_app_without_dir_is_not_buildable(self):
        app = config.App.from_json({"id": "published", "name": "Published"})
        self.assertFalse(app.buildable)
        self.assertEqual(app.to_json(), {"id": "published", "name": "Published"})

    def test_locations_follow_environment(self):
        with temp_store() as root:
            self.assertEqual(config.builds_dir("x"), root / "home" / "builds" / "x")
            self.assertEqual(config.derived_data("x"), root / "cache" / "DerivedData" / "x")


if __name__ == "__main__":
    unittest.main()
