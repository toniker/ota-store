import hashlib
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase, mock

from ota_store import builds, config
from ota_store.config import App
from ota_store.fingerprint import Fingerprint
from tests.helpers import make_ipa, mobileprovision, png, temp_store


def fingerprint(seed: str, source: str = "main@abc1234") -> Fingerprint:
    return Fingerprint(key=hashlib.sha256(seed.encode()).hexdigest(), source=source, dirty=False)


class StoreTestCase(TestCase):
    def setUp(self):
        self._store = temp_store()
        self.root = self._store.__enter__()
        self.addCleanup(self._store.__exit__, None, None, None)
        self.app = App(id="trailmap", name="TrailMap", dir=self.root / "src", keep=3)

    def ipa(self, filename: str = "build.ipa", **options) -> Path:
        return make_ipa(self.root / "in" / filename, **options)


class InspectTests(StoreTestCase):
    def test_reads_info_and_profile(self):
        expires = datetime(2027, 11, 21, 10, 0, tzinfo=timezone.utc)
        info = builds.inspect_ipa(self.ipa(bundle_id="com.example.TrailMap", name="TrailMap", version="2.1",
                                           build="7", expires=expires, devices=3))
        self.assertEqual(info["bundle_id"], "com.example.TrailMap")
        self.assertEqual((info["name"], info["version"], info["build"]), ("TrailMap", "2.1", "7"))
        self.assertEqual(info["profile"], {"name": "iOS Team Provisioning Profile: *", "team": "Example Team",
                                           "expiresAt": "2027-11-21T10:00:00+00:00", "devices": 3})

    def test_no_profile(self):
        self.assertIsNone(builds.inspect_ipa(self.ipa(profile=False))["profile"])

    def test_profile_for_all_devices_has_no_device_count(self):
        data = mobileprovision(datetime(2027, 1, 1)).replace(
            b"<key>Name</key>", b"<key>ProvisionsAllDevices</key><true/><key>Name</key>")
        self.assertIsNone(builds.read_profile(data)["devices"])
        self.assertIsNone(builds.read_profile(b"no plist in here"))

    def test_rejects_what_isnt_an_ipa(self):
        not_zip = self.root / "notes.ipa"
        not_zip.write_text("hello")
        no_app = self.root / "empty.ipa"
        with zipfile.ZipFile(no_app, "w") as archive:
            archive.writestr("README", "nothing")
        for path in (not_zip, no_app, self.root / "missing.ipa"):
            with self.assertRaises(builds.InvalidIPA):
                builds.inspect_ipa(path)


class StoreTests(StoreTestCase):
    def test_store_and_find(self):
        source = self.ipa(bundle_id="com.other.Thing")
        print_ = fingerprint("one")
        build = builds.store(self.app, source, print_, 12.345)

        self.assertFalse(source.exists(), "the .ipa is moved in")
        self.assertEqual(build.key, print_.key[:16])
        self.assertEqual(build.path, config.builds_dir("trailmap") / build.key)
        self.assertTrue(build.ipa_path.is_file())
        self.assertEqual(build.size, build.ipa_path.stat().st_size)
        self.assertEqual((build.app_id, build.bundle_id), ("trailmap", "com.other.Thing"))
        self.assertEqual((build.duration, build.source, build.configuration), (12.3, "main@abc1234", "Debug"))
        self.assertFalse(build.published)
        self.assertIsNone(build.icon_path)

        meta = json.loads((build.path / "meta.json").read_text())
        self.assertEqual(meta["appId"], "trailmap")
        self.assertEqual(meta["fingerprint"], print_.key)
        self.assertEqual(builds.get("trailmap", build.key), build)
        self.assertEqual(builds.find("trailmap", print_.key), build)
        self.assertIsNone(builds.find("trailmap", fingerprint("two").key))
        self.assertEqual(builds.latest("trailmap"), build)
        self.assertEqual(builds.list_builds("trailmap"), [build])
        self.assertEqual([p.name for p in config.builds_dir("trailmap").iterdir()], [build.key])

    def test_invalid_ipa_stays_put_and_stores_nothing(self):
        bad = self.root / "bad.ipa"
        bad.write_text("nope")
        with self.assertRaises(builds.InvalidIPA):
            builds.store(self.app, bad, fingerprint("x"), 1)
        self.assertTrue(bad.exists())
        self.assertEqual(builds.list_builds("trailmap"), [])

    def test_icon_from_icons_module(self):
        def extract(app, ipa, dest):
            self.assertEqual(app, self.app)
            self.assertTrue(Path(ipa).is_file())
            Path(dest).write_bytes(png(2, 2))
            return True
        with mock.patch("ota_store.icons.extract_icon", extract):
            build = builds.store(self.app, self.ipa(), fingerprint("icon"), 1)
        self.assertTrue(build.has_icon)
        self.assertEqual(build.icon_path.read_bytes(), png(2, 2))

        def broken(app, ipa, dest):
            raise RuntimeError("boom")
        with mock.patch("ota_store.icons.extract_icon", broken):
            self.assertFalse(builds.store(self.app, self.ipa(), fingerprint("broken icon"), 1).has_icon)

    def test_rebuild_with_same_key_replaces_it_while_a_download_goes_on(self):
        with mock.patch("ota_store.icons.extract_icon", lambda app, ipa, dest: Path(dest).write_bytes(png()) > 0):
            old = builds.store(self.app, self.ipa(version="1.0"), fingerprint("same"), 1)
        old_bytes = old.ipa_path.read_bytes()
        download = open(old.ipa_path, "rb")
        self.addCleanup(download.close)
        download.read(10)

        new = builds.store(self.app, self.ipa(version="1.1"), fingerprint("same"), 2)
        self.assertEqual(new.key, old.key)
        self.assertEqual(builds.get("trailmap", new.key).version, "1.1")
        self.assertFalse(builds.get("trailmap", new.key).has_icon)
        self.assertFalse((new.path / "icon.png").exists())
        self.assertEqual(old_bytes[10:], download.read(), "the open download still reads the old file whole")
        self.assertEqual(len(builds.list_builds("trailmap")), 1)
        self.assertEqual([p.name for p in config.builds_dir("trailmap").iterdir()], [new.key])

    def test_prunes_to_keep_by_last_used(self):
        stored = [builds.store(self.app, self.ipa(build=str(n)), fingerprint(str(n)), 1) for n in range(3)]
        builds.mark_used(stored[0])
        fourth = builds.store(self.app, self.ipa(build="3"), fingerprint("3"), 1)
        keys = {build.key for build in builds.list_builds("trailmap")}
        self.assertEqual(keys, {stored[0].key, stored[2].key, fourth.key})
        self.assertEqual(builds.list_builds("trailmap")[0], fourth, "newest built_at first")

    def test_keep_one(self):
        self.app.keep = 1
        builds.store(self.app, self.ipa(), fingerprint("a"), 1)
        latest = builds.store(self.app, self.ipa(), fingerprint("b"), 1)
        self.assertEqual(builds.list_builds("trailmap"), [latest])

    def test_installable_needs_ipa_and_unexpired_profile(self):
        now = datetime.now(timezone.utc)
        soon = builds.store(self.app, self.ipa(expires=now + timedelta(minutes=30)), fingerprint("soon"), 1)
        later = builds.store(self.app, self.ipa(expires=now + timedelta(days=3)), fingerprint("later"), 1)
        bare = builds.store(self.app, self.ipa(profile=False), fingerprint("bare"), 1)
        self.assertFalse(soon.installable())
        self.assertTrue(later.installable())
        self.assertFalse(later.installable(now + timedelta(days=3)))
        self.assertTrue(bare.installable())
        self.assertEqual(builds.latest("trailmap"), bare)
        bare.ipa_path.unlink()
        self.assertFalse(bare.installable())
        self.assertEqual(builds.latest("trailmap"), later)

    def test_mark_used_updates_meta(self):
        build = builds.store(self.app, self.ipa(), fingerprint("a"), 1)
        before = build.last_used_at
        builds.mark_used(build)
        self.assertGreaterEqual(builds.get("trailmap", build.key).last_used_at, before)
        self.assertEqual(builds.get("trailmap", build.key).last_used_at, build.last_used_at)
        builds.delete_all("trailmap")
        builds.mark_used(build)  # gone: nothing to do, and no error

    def test_skips_unreadable_and_rejects_bad_ids(self):
        build = builds.store(self.app, self.ipa(), fingerprint("a"), 1)
        junk = config.builds_dir("trailmap") / "0123456789abcdef"
        junk.mkdir()
        (junk / "meta.json").write_text("{not json")
        (config.builds_dir("trailmap") / ".new-leftover").mkdir()
        self.assertEqual(builds.list_builds("trailmap"), [build])
        self.assertIsNone(builds.get("trailmap", "0123456789abcdef"))
        self.assertIsNone(builds.get("trailmap", "../../config"))
        self.assertIsNone(builds.get("../trailmap", build.key))
        self.assertEqual(builds.list_builds(".."), [])

    def test_clears_stale_temporary_folders(self):
        leftover = config.builds_dir("trailmap") / ".new-old"
        leftover.mkdir(parents=True)
        recent = config.builds_dir("trailmap") / ".new-recent"
        recent.mkdir()
        os.utime(leftover, (0, 0))
        builds.store(self.app, self.ipa(), fingerprint("a"), 1)
        self.assertFalse(leftover.exists())
        self.assertTrue(recent.exists(), "it may be a store in progress")

    def test_delete_all(self):
        builds.store(self.app, self.ipa(), fingerprint("a"), 1)
        builds.delete_all("trailmap")
        self.assertEqual(builds.list_builds("trailmap"), [])
        self.assertFalse(config.builds_dir("trailmap").exists())
        builds.delete_all("trailmap")


class PublishTests(StoreTestCase):
    def test_publish_copies_and_keys_by_content(self):
        source = self.ipa(bundle_id="com.example.PocketJournal", name="PocketJournal")
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        build = builds.publish("pocketjournal", source, name="The PocketJournal")
        self.assertTrue(source.exists(), "publish copies")
        self.assertEqual((build.key, build.fingerprint), (sha[:16], sha))
        self.assertEqual((build.name, build.bundle_id, build.app_id), ("The PocketJournal", "com.example.PocketJournal", "pocketjournal"))
        self.assertTrue(build.published)
        self.assertIsNone(build.duration)
        self.assertIsNone(build.configuration)
        self.assertEqual(builds.publish("pocketjournal", source).key, build.key)
        self.assertEqual(len(builds.list_builds("pocketjournal")), 1)
        self.assertEqual(builds.latest("pocketjournal").name, "PocketJournal")
        self.assertFalse(config.config_path().exists(), "publish leaves the config alone")

    def test_publish_uses_the_registered_keep(self):
        config.config_path().parent.mkdir(parents=True)
        config.config_path().write_text(json.dumps({"apps": [{"id": "pocketjournal", "keep": 1}]}))
        builds.publish("pocketjournal", self.ipa(build="1"))
        second = builds.publish("pocketjournal", self.ipa(build="2"))
        self.assertEqual(builds.list_builds("pocketjournal"), [second])

    def test_publish_passes_no_app_to_icons(self):
        seen = []
        with mock.patch("ota_store.icons.extract_icon", lambda app, ipa, dest: seen.append(app) or False):
            builds.publish("pocketjournal", self.ipa())
        self.assertEqual(seen, [None])
