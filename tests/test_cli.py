"""The `ota-store` commands, run through cli.main with captured output."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from ota_store import cli, config
from ota_store.config import App, load_config, save_config
from ota_store.service import Service
from tests.helpers import fake_build_command, make_ipa, temp_store


def run(*argv: str) -> Tuple[int, str, str]:
    """Runs the CLI; returns its exit code, stdout and stderr."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = cli.main(list(argv))
        except SystemExit as exit:
            code = exit.code if isinstance(exit.code, int) else 1
    return code, stdout.getvalue(), stderr.getvalue()


def free_port() -> int:
    """A port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class StoreTestCase(unittest.TestCase):
    """A temporary store, no Tailscale, and a public URL from the environment."""

    def setUp(self):
        self.store = self._enter(temp_store())
        self._enter(mock.patch.dict(os.environ, {"OTA_STORE_URL": "https://apps.example.ts.net"}))
        os.environ.pop("OTA_STORE_PORT", None)
        self._enter(mock.patch("ota_store.doctor.tailscale_urls", return_value=[]))
        self._enter(mock.patch.object(cli, "POLL_INTERVAL", 0.01))

    def _enter(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def register(self, app: App) -> App:
        current = load_config(apply_environment=False)
        current.apps[app.id] = app
        save_config(current)
        return app

    def project_folder(self, name: str = "WeatherDeck", kind: str = ".xcodeproj") -> Path:
        folder = self.store / "src" / name
        (folder / f"{name}{kind}").mkdir(parents=True)
        return folder


class ParsingTests(unittest.TestCase):
    def test_commands_parse(self):
        parser = cli.make_parser()
        args = parser.parse_args(["add", "dir", "--id", "x", "--input", "a", "--input", "b"])
        self.assertEqual((args.dir, args.id, args.input), ("dir", "x", ["a", "b"]))
        self.assertTrue(parser.parse_args(["build", "x", "--force"]).force)
        self.assertTrue(parser.parse_args(["remove", "x", "--purge"]).purge)
        self.assertEqual(parser.parse_args(["service", "restart"]).action, "restart")
        self.assertEqual(parser.parse_args(["serve", "--port", "9000"]).port, 9000)
        self.assertEqual(parser.parse_args(["add"]).dir, ".")

    def test_usage_errors_exit_2(self):
        for argv in ([], ["bogus"], ["build"], ["service", "explode"], ["publish", "x.ipa"]):
            code, _, err = run(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("usage:", err)

    def test_xcode_build_gets_every_argument(self):
        with mock.patch("ota_store.xcode_build.main", return_value=0) as main:
            self.assertEqual(run("xcode-build", "--anything", "x")[0], 0)
        main.assert_called_once_with(["--anything", "x"])

    def test_slugify(self):
        self.assertEqual(cli.slugify("WeatherDeck"), "weatherdeck")
        self.assertEqual(cli.slugify("My App 2!"), "my-app-2")

    def test_duration(self):
        self.assertEqual(cli.duration(3), "0:03")
        self.assertEqual(cli.duration(75), "1:15")
        self.assertEqual(cli.duration(3725), "1:02:05")


class AddTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.find_project = self._enter(mock.patch.object(
            cli, "find_project", side_effect=lambda folder: next(folder.glob("*.xcodeproj"))))
        self.find_scheme = self._enter(mock.patch.object(
            cli, "find_scheme", side_effect=lambda project, preferred=None: preferred or project.stem))

    def test_add_registers_the_discovered_project(self):
        folder = self.project_folder()
        code, out, err = run("add", str(folder))
        self.assertEqual(code, 0, err)
        self.assertIn("Registered weatherdeck (WeatherDeck)", out)
        self.assertIn("ota-store build weatherdeck", out)
        saved = json.loads(config.config_path().read_text())
        self.assertEqual(saved["apps"], [{"id": "weatherdeck", "name": "WeatherDeck", "dir": str(folder),
                                          "project": "WeatherDeck.xcodeproj", "scheme": "WeatherDeck"}])

    def test_add_options(self):
        folder = self.project_folder()
        (folder / "shared").mkdir()
        code, _, err = run("add", str(folder), "--id", "pes", "--name", "Pes", "--scheme", "Other",
                           "--configuration", "Release", "--input", "shared")
        self.assertEqual(code, 0, err)
        app = load_config().app("pes")
        self.assertEqual((app.name, app.scheme, app.configuration), ("Pes", "Other", "Release"))
        self.assertEqual(app.inputs, [folder / "shared"])

    def test_add_with_command_skips_discovery(self):
        folder = self.store / "src" / "custom"
        folder.mkdir(parents=True)
        code, out, err = run("add", str(folder), "--command", "make ipa")
        self.assertEqual(code, 0, err)
        self.find_project.assert_not_called()
        app = load_config().app("custom")
        self.assertEqual((app.name, app.command, app.project), ("custom", "make ipa", None))

    def test_duplicate_id_is_refused(self):
        run("add", str(self.project_folder()))
        other = self.store / "elsewhere"
        (other / "WeatherDeck.xcodeproj").mkdir(parents=True)
        code, _, err = run("add", str(other))
        self.assertEqual(code, 1)
        self.assertIn("already an app with id 'weatherdeck'", err)
        self.assertIn("--id", err)
        self.assertEqual(len(load_config().apps), 1)

    def test_duplicate_folder_is_refused(self):
        folder = self.project_folder()
        run("add", str(folder))
        code, _, err = run("add", str(folder), "--id", "again")
        self.assertEqual(code, 1)
        self.assertIn("already registered as 'weatherdeck'", err)

    def test_bad_paths_are_refused(self):
        code, _, err = run("add", str(self.store / "missing"))
        self.assertEqual(code, 1)
        self.assertIn("Not a folder", err)
        code, _, err = run("add", str(self.project_folder()), "--input", "nope")
        self.assertEqual(code, 1)
        self.assertIn("Input not found", err)
        self.assertFalse(config.config_path().exists())

    def test_bad_id_is_refused(self):
        code, _, err = run("add", str(self.project_folder()), "--id", "Bad Id")
        self.assertEqual(code, 1)
        self.assertIn("can't be an id", err)


class DiscoveryFallbackTests(StoreTestCase):
    def test_finds_the_workspace_first(self):
        folder = self.project_folder("App")
        (folder / "App.xcworkspace").mkdir()
        self.assertEqual(cli._find_project(folder), folder / "App.xcworkspace")

    def test_ambiguity_and_absence_say_what_to_do(self):
        folder = self.project_folder("One")
        (folder / "Two.xcodeproj").mkdir()
        with self.assertRaises(cli.CommandError) as caught:
            cli._find_project(folder)
        self.assertIn("--project", caught.exception.hint)
        with self.assertRaises(cli.CommandError):
            cli._find_project(self.store)

    def test_preferred_scheme_wins(self):
        self.assertEqual(cli._find_scheme(Path("/x/App.xcodeproj"), "Mine"), "Mine")


class StubServer:
    """A stand-in for the store's API: answers install and status from a script of Status objects."""

    def __init__(self, install: Dict[str, Any], statuses: List[Dict[str, Any]], install_code: int = 200,
                 raw_404: bool = False):
        self.requests: List[Tuple[str, str, Optional[str]]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, code: int, body: Any):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                stub.requests.append(("POST", self.path, self.headers.get("Host")))
                if raw_404:
                    return self.answer(404, b"<html>Not Found</html>")
                self.answer(install_code, install)

            def do_GET(self):
                stub.requests.append(("GET", self.path, self.headers.get("Host")))
                self.answer(200, statuses.pop(0) if len(statuses) > 1 else statuses[0])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


BUILD = {"key": "a1b2c3d4e5f60718", "version": "1.2", "build": "7",
         "installUrl": "itms-services://?action=download-manifest&url=https%3A%2F%2Fapps.example.ts.net%2Fapps%2Ffake"}


class BuildThroughServerTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.register(App(id="fake", name="Fake", dir=self.store, command="true"))

    def serve(self, *args, **kwargs) -> StubServer:
        stub = StubServer(*args, **kwargs)
        self.addCleanup(stub.close)
        os.environ["OTA_STORE_PORT"] = str(stub.port)
        return stub

    def test_follows_the_steps_until_done(self):
        stub = self.serve({"state": "queued", "step": "Waiting"}, [
            {"state": "building", "step": "Preparing", "elapsed": 1},
            {"state": "building", "step": "Building Fake", "elapsed": 2},
            {"state": "building", "step": "Building Fake", "elapsed": 3},
            {"state": "done", "elapsed": 41, "build": BUILD},
        ])
        code, out, err = run("build", "fake", "--force")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual([line.split("  ", 1)[1].strip() for line in lines[:3]],
                         ["Waiting", "Preparing", "Building Fake"])
        self.assertIn("Done: Fake 1.2 (7), built in 0:41", out)
        self.assertIn("Install: itms-services://", out)
        self.assertIn("https://apps.example.ts.net/#/apps/fake", out)
        self.assertEqual(stub.requests[0][:2], ("POST", "/api/apps/fake/install?force=1"))
        self.assertTrue(all(path == "/api/apps/fake/status" for _, path, _ in stub.requests[1:]))

    def test_cache_hit(self):
        self.serve({"state": "done", "cached": True, "elapsed": 0, "build": BUILD}, [{"state": "idle"}])
        code, out, _ = run("build", "fake")
        self.assertEqual(code, 0)
        self.assertIn("from a stored build, nothing changed", out)

    def test_failure_prints_the_error_and_log(self):
        self.serve({"state": "building", "step": "Building Fake"},
                   [{"state": "failed", "error": "cannot find 'Foo' in scope"}])
        code, out, _ = run("build", "fake")
        self.assertEqual(code, 1)
        self.assertIn("Failed: cannot find 'Foo' in scope", out)
        self.assertIn(str(config.log_path("fake")), out)

    def test_server_errors_are_shown(self):
        self.serve({"error": "No app 'fake'"}, [{}], install_code=404)
        code, _, err = run("build", "fake")
        self.assertEqual(code, 1)
        self.assertIn("No app 'fake'", err)

    def test_an_older_server_is_recognised(self):
        self.serve({}, [{}], raw_404=True)
        code, _, err = run("build", "fake")
        self.assertEqual(code, 1)
        self.assertIn("isn't ota-store", err)

    def test_public_host_is_sent_when_it_comes_from_tailscale(self):
        os.environ.pop("OTA_STORE_URL")
        stub = self.serve({"state": "done", "cached": True, "build": BUILD}, [{"state": "idle"}])
        with mock.patch("ota_store.doctor.tailscale_urls", return_value=["https://mac.example.ts.net"]):
            run("build", "fake")
        self.assertEqual(stub.requests[0][2], "mac.example.ts.net")

    def test_unknown_and_unbuildable_apps(self):
        self.register(App(id="published", name="Published"))
        code, _, err = run("build", "nope")
        self.assertEqual(code, 1)
        self.assertIn("ota-store list", err)
        code, _, err = run("build", "published")
        self.assertEqual(code, 1)
        self.assertIn("ota-store publish", err)


class BuildInProcessTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        os.environ["OTA_STORE_PORT"] = str(free_port())
        self.source = self.store / "src"
        self.source.mkdir()
        (self.source / "main.swift").write_text("print(1)\n")

    def test_builds_then_reuses(self):
        self.register(App(id="fake", name="Fake", dir=self.source, command=fake_build_command(version="2.0")))
        code, out, err = run("build", "fake")
        self.assertEqual(code, 0, err)
        self.assertIn("building here", out)
        self.assertIn("Preparing", out)
        self.assertIn("Done: Fake 2.0 (1), built in", out)
        self.assertIn("Install: itms-services://?action=download-manifest&url=https%3A%2F%2Fapps.example.ts.net", out)

        code, out, _ = run("build", "fake")
        self.assertEqual(code, 0)
        self.assertIn("nothing changed", out)

        code, out, _ = run("list")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"fake\s+Fake\s+current\s+2\.0 \(1\)\s+just now")

    def test_failure(self):
        self.register(App(id="fake", name="Fake", dir=self.source, command=fake_build_command(fail="1")))
        code, out, _ = run("build", "fake")
        self.assertEqual(code, 1)
        self.assertIn("Failed: cannot find 'Foo' in scope", out)
        code, out, _ = run("log", "fake")
        self.assertEqual(code, 0)
        self.assertIn("BUILD FAILED", out)


class OtherCommandTests(StoreTestCase):
    def test_list_without_apps(self):
        code, out, _ = run("list")
        self.assertEqual((code, out.strip()), (0, "No apps yet. Register one: ota-store add ~/path/to/app"))

    def test_list_json_and_states(self):
        self.register(App(id="gone", name="Gone", dir=self.store / "missing"))
        self.register(App(id="pub", name="Pub"))
        code, out, _ = run("list", "--json")
        self.assertEqual(code, 0)
        rows = {row["id"]: row for row in json.loads(out)}
        self.assertIsNone(rows["pub"]["freshness"])
        self.assertTrue(rows["gone"]["problem"])
        code, out, _ = run("list")
        self.assertIn("gone: ", out)
        self.assertRegex(out, r"pub\s+Pub\s+published")

    def test_publish_registers_without_a_folder(self):
        ipa = make_ipa(self.store / "Thing.ipa", bundle_id="com.example.Thing", name="Thing", version="3.1")
        code, out, err = run("publish", str(ipa), "--id", "thing")
        self.assertEqual(code, 0, err)
        self.assertIn("Registered thing (Thing), without a folder", out)
        self.assertIn("Published thing: Thing 3.1 (1)", out)
        self.assertIn("Install: itms-services://", out)
        app = load_config().app("thing")
        self.assertIsNone(app.dir)
        code, out, _ = run("publish", str(ipa), "--id", "thing")
        self.assertNotIn("Registered", out)

    def test_publish_refuses_bad_files(self):
        bad = self.store / "bad.ipa"
        bad.write_text("nope")
        code, _, err = run("publish", str(bad), "--id", "thing")
        self.assertEqual(code, 1)
        self.assertIn("isn't a usable .ipa", err)
        code, _, err = run("publish", str(self.store / "none.ipa"), "--id", "thing")
        self.assertEqual(code, 1)
        self.assertIsNone(load_config().app("thing"))

    def test_remove_and_purge(self):
        self.register(App(id="fake", name="Fake", dir=self.store))
        builds = config.builds_dir("fake") / "k"
        builds.mkdir(parents=True)
        config.derived_data("fake").mkdir(parents=True)
        code, out, _ = run("remove", "fake")
        self.assertEqual(code, 0)
        self.assertIn("--purge", out)
        self.assertIsNone(load_config().app("fake"))
        self.assertTrue(builds.exists())
        code, _, _ = run("remove", "fake")
        self.assertEqual(code, 1)
        code, out, _ = run("remove", "fake", "--purge")
        self.assertEqual(code, 0)
        self.assertFalse(config.builds_dir("fake").exists())
        self.assertFalse(config.derived_data("fake").exists())

    def test_log(self):
        code, _, err = run("log", "fake")
        self.assertEqual(code, 1)
        self.assertIn("ota-store build fake", err)

    def test_url(self):
        code, out, _ = run("url")
        self.assertEqual((code, out.strip()), (0, "https://apps.example.ts.net"))
        os.environ.pop("OTA_STORE_URL")
        code, _, err = run("url")
        self.assertEqual(code, 1)
        self.assertIn("tailscale serve --bg --https=443 http://127.0.0.1:8765", err)
        with mock.patch("ota_store.doctor.tailscale_urls", return_value=["https://mac.example.ts.net"]):
            self.assertEqual(run("url")[1].strip(), "https://mac.example.ts.net")

    def test_bad_config_says_what_to_fix(self):
        config.config_path().parent.mkdir(parents=True)
        config.config_path().write_text("{nope")
        code, _, err = run("list")
        self.assertEqual(code, 1)
        self.assertIn(f"Fix {config.config_path()}", err)

    def test_service_commands_use_the_injected_service(self):
        calls: List[List[str]] = []

        def runner(arguments):
            calls.append(arguments)
            import subprocess
            loaded = any(call[1] == "bootstrap" for call in calls[:-1])
            return subprocess.CompletedProcess(arguments, 0 if loaded or arguments[1] != "print" else 113,
                                               "\tstate = running\n\tpid = 42\n" if loaded else "", "")

        folder = self.store / "LaunchAgents"
        with mock.patch.object(cli, "make_service", lambda: Service(folder=folder, run=runner, uid=501)):
            code, out, err = run("service", "install")
            self.assertEqual(code, 0, err)
            self.assertIn("running (pid 42)", out)
            self.assertTrue((folder / "local.ota-store.plist").is_file())
            code, out, _ = run("service", "status")
            self.assertIn("local.ota-store: running (pid 42)", out)
        self.assertIn(["launchctl", "bootstrap", "gui/501", str(folder / "local.ota-store.plist")], calls)


if __name__ == "__main__":
    unittest.main()
