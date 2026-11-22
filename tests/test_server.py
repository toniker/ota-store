import io
import json
import os
import plistlib
import socket
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Tuple
from unittest import TestCase, mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from ota_store import builds, config, server
from ota_store.builder import Builder
from ota_store.config import Config
from tests.helpers import fake_build_command, make_ipa, png, temp_store

WAIT = 15


class ServerTestCase(TestCase):
    """A real server on a free port, against a temporary store."""

    def setUp(self):
        store = temp_store()
        self.root = store.__enter__()
        self.addCleanup(store.__exit__, None, None, None)
        patcher = mock.patch.dict(os.environ, {"OTA_STORE_SHELL": "/bin/sh"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("OTA_STORE_URL", "OTA_STORE_TITLE"):
            os.environ.pop(key, None)
        self.counter = self.root / "counter"
        self.web = self.root / "web"
        self.web.mkdir()
        web = mock.patch.object(server, "WEB", self.web)
        web.start()
        self.addCleanup(web.stop)

        # The server logs requests to stderr; keep them out of the test output (and readable by tests).
        self.stderr = io.StringIO()
        quiet = mock.patch("sys.stderr", self.stderr)
        quiet.start()
        self.addCleanup(quiet.stop)

        self.builder = Builder(timeout=30, kill_grace=0.2)
        self.server = server.make_server("127.0.0.1", 0, builder=self.builder, freshness_ttl=0)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        for job in self.builder.jobs():
            self.builder.cancel(job.app.id)
            self.builder.wait(job, timeout=WAIT)

    # MARK: - Config

    def configure(self, *apps: Dict[str, Any], **settings: Any) -> None:
        path = config.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**settings, "apps": list(apps)}))

    def fake_app(self, app_id: str = "fake", **fake: str) -> Dict[str, Any]:
        folder = self.root / "src" / app_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "main.swift").write_text("print(1)\n")
        return {"id": app_id, "name": app_id.title(), "dir": str(folder),
                "command": fake_build_command(counter=self.counter, **fake)}

    def builds_run(self) -> int:
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    # MARK: - Requests

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, path: str, method: str = "GET") -> Tuple[int, Dict[str, str], bytes]:
        try:
            with urlopen(Request(self.url(path), method=method, data=b"" if method == "POST" else None),
                         timeout=WAIT) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def json(self, path: str, method: str = "GET", status: int = 200) -> Any:
        code, headers, body = self.request(path, method)
        self.assertEqual(code, status, body)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        return json.loads(body)

    def raw(self, request: bytes) -> bytes:
        """Everything the server sends back for `request`, until it closes the connection."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=WAIT) as connection:
            connection.sendall(request)
            chunks = []
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

    def install(self, app_id: str = "fake", force: bool = False) -> Dict[str, Any]:
        return self.json(f"/api/apps/{app_id}/install" + ("?force=1" if force else ""), "POST")

    def until_finished(self, app_id: str = "fake") -> Dict[str, Any]:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            status = self.json(f"/api/apps/{app_id}/status")
            if status["state"] in ("done", "failed"):
                return status
            time.sleep(0.05)
        self.fail("the build didn't finish")

    def app_entry(self, app_id: str = "fake") -> Dict[str, Any]:
        return next(app for app in self.json("/api/apps")["apps"] if app["id"] == app_id)

    def manifest_path(self, build: Dict[str, Any]) -> str:
        url = parse_qs(urlsplit(build["installUrl"]).query)["url"][0]
        self.assertTrue(url.startswith(f"http://127.0.0.1:{self.port}/"), url)
        return urlsplit(url).path


class TapTests(ServerTestCase):
    def test_tap_builds_then_installs(self):
        self.configure(self.fake_app())
        self.assertEqual(self.json("/api/apps/fake/status"), {"state": "idle"})
        self.assertEqual(self.app_entry()["freshness"], "unbuilt")

        status = self.install()
        self.assertIn(status["state"], ("queued", "building"))
        self.assertNotIn("cached", status)
        status = self.until_finished()
        self.assertEqual(status["state"], "done", status)
        build = status["build"]
        self.assertEqual(build["bundleId"], "com.example.Fake")
        self.assertTrue(build["installable"])
        self.assertEqual(build["devices"], 2)
        self.assertRegex(build["key"], r"^[0-9a-f]{16}$")
        self.assertTrue(build["installUrl"].startswith("itms-services://?action=download-manifest&url=http%3A%2F%2F"))

        code, headers, body = self.request(self.manifest_path(build))
        self.assertEqual(code, 200)
        item = plistlib.loads(body)["items"][0]
        self.assertEqual(item["metadata"], {"bundle-identifier": "com.example.Fake", "bundle-version": "1.0",
                                            "kind": "software", "title": "Fake"})
        package = item["assets"][0]
        self.assertEqual(package["kind"], "software-package")

        code, headers, body = self.request(urlsplit(package["url"]).path)
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        stored = builds.get("fake", build["key"])
        self.assertEqual(body, stored.ipa_path.read_bytes())
        self.assertEqual(int(headers["Content-Length"]), build["size"])

        entry = self.app_entry()
        self.assertEqual(entry["freshness"], "current")
        self.assertEqual(entry["latest"]["key"], build["key"])
        self.assertEqual(entry["bundleId"], "com.example.Fake")
        self.assertEqual(entry["status"]["state"], "done")

    def test_second_tap_is_a_cache_hit(self):
        self.configure(self.fake_app())
        self.install()
        first = self.until_finished()
        status = self.install()
        self.assertEqual(status["state"], "done")
        self.assertTrue(status["cached"])
        self.assertEqual(status["build"]["key"], first["build"]["key"])
        self.assertEqual(self.builds_run(), 1)
        # A phone still following the first build can install it.
        followed = self.json("/api/apps/fake/status")
        self.assertEqual((followed["state"], followed["build"]["key"]), ("done", first["build"]["key"]))

    def test_a_change_makes_the_next_tap_build(self):
        app = self.fake_app()
        self.configure(app)
        self.install()
        self.until_finished()
        (Path(app["dir"]) / "main.swift").write_text("print(2)\n")
        entry = self.app_entry()
        self.assertEqual(entry["freshness"], "changed")
        self.assertIsNotNone(entry["latest"], "the old build is still there to install")
        self.assertIn(self.install()["state"], ("queued", "building"))
        self.assertEqual(self.until_finished()["state"], "done")
        self.assertEqual(self.builds_run(), 2)
        self.assertEqual(self.app_entry()["freshness"], "current")

    def test_force_rebuilds(self):
        self.configure(self.fake_app())
        self.install()
        first = self.until_finished()["build"]
        self.assertIn(self.install(force=True)["state"], ("queued", "building"))
        second = self.until_finished()["build"]
        self.assertEqual(second["key"], first["key"])
        self.assertEqual(self.builds_run(), 2)

    def test_failure_reports_the_error(self):
        self.configure(self.fake_app(fail="1"), self.fake_app("empty", no_output="1"))
        self.install()
        status = self.until_finished()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"], "cannot find 'Foo' in scope")
        self.assertNotIn("build", status)
        self.install("empty")
        self.assertIn("without writing an .ipa", self.until_finished("empty")["error"])
        self.assertEqual(self.app_entry()["freshness"], "unbuilt")

    def test_expired_profile_rebuilds(self):
        self.configure(self.fake_app(expired="1"))
        self.install()
        self.assertFalse(self.until_finished()["build"]["installable"])
        entry = self.app_entry()
        self.assertEqual(entry["freshness"], "changed")
        self.assertIsNone(entry["latest"])
        self.assertIn(self.install()["state"], ("queued", "building"))
        self.until_finished()
        self.assertEqual(self.builds_run(), 2)

    def test_concurrent_taps_join_one_job(self):
        self.configure(self.fake_app(sleep="0.3"))
        barrier = threading.Barrier(4)
        states = []

        def tap():
            barrier.wait()
            states.append(self.install()["state"])

        threads = [threading.Thread(target=tap) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(states), 4)
        self.until_finished()
        self.assertEqual(self.builds_run(), 1)

    def test_queue_shows_what_it_waits_for(self):
        slow = self.fake_app("slow", sleep="30")
        slow["name"] = "Slow App"
        self.configure(slow, self.fake_app())
        self.install("slow")
        deadline = time.monotonic() + WAIT
        while self.json("/api/apps/slow/status")["state"] != "building":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        status = self.install()
        self.assertEqual(status["state"], "queued")
        self.assertEqual(status["step"], "Waiting for Slow App")
        self.assertEqual(self.json("/api/apps/fake/status")["step"], "Waiting for Slow App")
        self.builder.cancel("slow")
        self.assertEqual(self.until_finished("slow")["error"], "Cancelled")
        self.assertEqual(self.until_finished()["state"], "done")

    def test_prunes_to_keep(self):
        app = self.fake_app()
        app["keep"] = 1
        self.configure(app)
        for n in range(2):
            (Path(app["dir"]) / "main.swift").write_text("x" * (n + 5))
            self.install()
            self.until_finished()
        self.assertEqual(len(self.json("/api/apps/fake")["builds"]), 1)

    def test_published_app_without_a_folder(self):
        self.configure({"id": "pocketjournal", "name": "PocketJournal"})
        self.assertEqual(self.json("/api/apps/pocketjournal/install", "POST", status=409).keys(), {"error"})
        ipa = make_ipa(self.root / "PocketJournal.ipa", bundle_id="com.example.PocketJournal", name="PocketJournal")
        with mock.patch("ota_store.icons.extract_icon", lambda app, ipa, dest: Path(dest).write_bytes(png()) > 0):
            published = builds.publish("pocketjournal", ipa)

        entry = self.app_entry("pocketjournal")
        self.assertFalse(entry["buildable"])
        self.assertIsNone(entry["freshness"])
        self.assertIsNone(entry["problem"])
        self.assertEqual(entry["latest"]["key"], published.key)
        self.assertEqual(entry["iconUrl"], "/apps/pocketjournal/icon.png")

        status = self.install("pocketjournal")
        self.assertEqual((status["state"], status["build"]["key"]), ("done", published.key))
        manifest = plistlib.loads(self.request(self.manifest_path(status["build"]))[2])
        kinds = [asset["kind"] for asset in manifest["items"][0]["assets"]]
        self.assertEqual(kinds, ["software-package", "display-image", "full-size-image"])
        self.assertEqual(self.request("/apps/pocketjournal/icon.png")[2], png())
        self.assertEqual(self.request(f"/apps/pocketjournal/{published.key}/icon.png")[2], png())


class APITests(ServerTestCase):
    def test_app_list_order_and_detail(self):
        self.configure(self.fake_app("old"), self.fake_app("new"), self.fake_app("never"),
                       {"id": "broken", "dir": str(self.root / "missing")}, title="Mine")
        for app_id in ("old", "new"):
            self.install(app_id)
            self.until_finished(app_id)
        listing = self.json("/api/apps")
        self.assertEqual(listing["title"], "Mine")
        self.assertEqual([app["id"] for app in listing["apps"]][:2], ["new", "old"])
        broken = next(app for app in listing["apps"] if app["id"] == "broken")
        self.assertIsNone(broken["freshness"])
        self.assertIn("folder not found", broken["problem"])
        self.assertEqual(next(app for app in listing["apps"] if app["id"] == "never")["freshness"], "unbuilt")

        detail = self.json("/api/apps/old")
        self.assertEqual(detail["commits"], [])
        self.assertEqual(detail["info"]["command"], self.fake_app("old")["command"])
        self.assertEqual(detail["info"]["configuration"], "Debug")
        self.assertEqual(len(detail["builds"]), 1)
        self.assertEqual(detail["freshness"], "current")
        with mock.patch.object(Path, "home", return_value=self.root):
            self.assertEqual(self.json("/api/apps/old")["info"]["dir"], "~/src/old")

    def test_freshness_is_cached(self):
        app = self.fake_app()
        self.configure(app)
        self.install()
        self.until_finished()
        self.server.freshness.ttl = 60
        self.assertEqual(self.app_entry()["freshness"], "current")
        (Path(app["dir"]) / "main.swift").write_text("changed\n")
        self.assertEqual(self.app_entry()["freshness"], "current", "within the cache time")
        self.server.freshness.ttl = 0
        self.assertEqual(self.app_entry()["freshness"], "changed")

    def test_log(self):
        self.configure(self.fake_app())
        self.assertEqual(self.json("/api/apps/fake/log", status=404).keys(), {"error"})
        self.install()
        self.until_finished()
        code, headers, body = self.request("/api/apps/fake/log")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn(b"==> Building Fake", body)

        config.log_path("fake").write_bytes(b"first line\n" + b"0123456789abcde\n" * 20000)
        body = self.request("/api/apps/fake/log")[2]
        self.assertLessEqual(len(body), server.LOG_TAIL)
        self.assertTrue(body.startswith(b"0123456789abcde\n"))

    def test_config_error_is_a_json_500(self):
        config.config_path().parent.mkdir(parents=True)
        config.config_path().write_text("{ half written")
        error = self.json("/api/apps", status=500)
        self.assertIn("isn't valid JSON", error["error"])
        self.configure(self.fake_app())
        self.assertEqual(len(self.json("/api/apps")["apps"]), 1)

    def test_not_found(self):
        self.configure(self.fake_app())
        self.assertEqual(self.request("/apps/fake/icon.png")[0], 404, "no build yet")
        self.install()
        key = self.until_finished()["build"]["key"]
        for path in ("/api/apps/nope", "/api/apps/nope/status", "/api/apps/fake/whatever", "/api/nothing"):
            self.assertIn("error", self.json(path, status=404), path)
        self.assertIn("error", self.json("/api/apps/nope/install", "POST", status=404))
        for path in ("/apps/nope/icon.png", f"/apps/nope/{key}/App.ipa", "/apps/fake/0000000000000000/App.ipa",
                     "/apps/fake/ABCDEF/App.ipa", f"/apps/fake/{key}/meta.json", "/static/missing.css", "/static/", "/nothing"):
            self.assertEqual(self.request(path)[0], 404, path)
        self.assertEqual(self.request(f"/apps/fake/{key}/App.ipa")[0], 200)

    def test_path_traversal(self):
        (self.web / "app.css").write_text("body {}")
        config.config_path().parent.mkdir(parents=True, exist_ok=True)
        for path in ("/static/../../home/config.json", "/static/..%2F..%2Fhome%2Fconfig.json",
                     "/static/%2e%2e/config.json", "/static/.hidden", "/static/sub/app.css",
                     "/apps/..%2F..%2Fhome/0000000000000000/App.ipa", "/apps/fake/..%2Fx/App.ipa",
                     "//etc/passwd", "/static//app.css"):
            response = self.raw(f"GET {path} HTTP/1.0\r\n\r\n".encode())
            self.assertTrue(response.startswith(b"HTTP/1.0 404"), (path, response[:80]))
        self.assertEqual(self.request("/static/app.css")[2], b"body {}")

    def test_static_files(self):
        (self.web / "index.html").write_text("<h1>Apps</h1>")
        (self.web / "app.js").write_text("1")
        (self.web / "favicon-32.png").write_bytes(png())
        code, headers, body = self.request("/")
        self.assertEqual((code, body), (200, b"<h1>Apps</h1>"))
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(self.request("/static/app.js")[1]["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(self.request("/favicon.ico")[2], png())

    def test_static_files_need_no_config(self):
        (self.web / "index.html").write_text("ok")
        config.config_path().parent.mkdir(parents=True)
        config.config_path().write_text("{ half written")
        self.assertEqual(self.request("/")[2], b"ok")

    def test_head_sends_no_body(self):
        self.configure(self.fake_app())
        self.install()
        build = self.until_finished()["build"]
        for path in (f"/apps/fake/{build['key']}/App.ipa", "/api/apps", self.manifest_path(build)):
            response = self.raw(f"HEAD {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
            head, _, body = response.partition(b"\r\n\r\n")
            self.assertTrue(head.startswith(b"HTTP/1.0 200"), head)
            self.assertIn(b"Content-Length: ", head)
            self.assertEqual(body, b"", path)
        response = self.raw(f"HEAD /apps/fake/{build['key']}/App.ipa HTTP/1.0\r\n\r\n".encode())
        self.assertIn(f"Content-Length: {build['size']}".encode(), response)
        response = self.raw(b"HEAD /nothing HTTP/1.0\r\n\r\n")
        self.assertTrue(response.startswith(b"HTTP/1.0 404"))
        self.assertEqual(response.partition(b"\r\n\r\n")[2], b"")

    def test_base_url_from_config(self):
        self.configure(self.fake_app(), url="https://apps.example.ts.net/")
        self.install()
        build = self.until_finished()["build"]
        url = parse_qs(urlsplit(build["installUrl"]).query)["url"][0]
        self.assertEqual(url, f"https://apps.example.ts.net/apps/fake/{build['key']}/manifest.plist")
        manifest = plistlib.loads(self.request(urlsplit(url).path)[2])
        self.assertEqual(manifest["items"][0]["assets"][0]["url"],
                         f"https://apps.example.ts.net/apps/fake/{build['key']}/App.ipa")


class LoggingTests(ServerTestCase):
    def capture_stderr(self) -> io.StringIO:
        self.stderr.seek(0)
        self.stderr.truncate()
        return self.stderr

    def test_api_requests_are_not_logged(self):
        (self.web / "app.css").write_text("x")
        self.configure()
        captured = self.capture_stderr()
        self.request("/api/apps")
        self.request("/api/apps/nope/status")
        self.assertEqual(captured.getvalue(), "")
        self.request("/static/app.css")
        self.assertIn("GET /static/app.css", captured.getvalue())

    def test_api_errors_are_logged(self):
        config.config_path().parent.mkdir(parents=True, exist_ok=True)
        config.config_path().write_text("{not json")
        captured = self.capture_stderr()
        self.assertEqual(self.request("/api/apps")[0], 500)
        self.assertIn("isn't valid JSON", captured.getvalue())

    def test_malformed_content_length_is_a_400(self):
        self.configure()
        connection = socket.create_connection(("127.0.0.1", self.port))
        connection.sendall(b"POST /api/apps/x/install HTTP/1.0\r\nContent-Length: abc\r\n\r\n")
        reply = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            reply += chunk
        connection.close()
        self.assertTrue(reply.startswith(b"HTTP/1.0 400"), reply[:80])
        self.assertNotIn("Traceback", self.stderr.getvalue())

    def test_client_hanging_up_mid_download_is_quiet(self):
        self.configure({"id": "big"})
        ipa = self.root / "Big.ipa"
        make_ipa(ipa)
        with zipfile.ZipFile(ipa, "a", zipfile.ZIP_STORED) as archive:
            archive.writestr("Payload/Fake.app/padding", os.urandom(8 << 20))
        key = builds.publish("big", ipa).key
        captured = self.capture_stderr()
        for _ in range(3):
            connection = socket.create_connection(("127.0.0.1", self.port))
            connection.sendall(f"GET /apps/big/{key}/App.ipa HTTP/1.0\r\n\r\n".encode())
            connection.recv(1024)
            # Reset rather than close politely, as a phone that drops off the network does.
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            connection.close()
        time.sleep(0.3)
        self.assertEqual(self.request("/api/apps")[0], 200, "still serving")
        self.assertNotIn("Traceback", captured.getvalue())
        self.assertNotIn("Error", captured.getvalue())


class BaseURLTests(TestCase):
    def test_rules(self):
        settings = Config()
        self.assertEqual(server.base_url_for("apps.example.ts.net", settings), "https://apps.example.ts.net")
        self.assertEqual(server.base_url_for("mac.local:8765", settings), "https://mac.local:8765")
        self.assertEqual(server.base_url_for("localhost:8765", settings), "http://localhost:8765")
        self.assertEqual(server.base_url_for("127.0.0.1:8765", settings), "http://127.0.0.1:8765")
        self.assertEqual(server.base_url_for("[::1]:8765", settings), "http://[::1]:8765")
        self.assertEqual(server.base_url_for(None, settings), "http://127.0.0.1:8765")
        self.assertEqual(server.base_url_for("localhost", Config(url="https://x.ts.net")), "https://x.ts.net")
