"""Doctor checks and the `tailscale serve` parsing, with injected commands and HTTP."""

from __future__ import annotations

import json
import plistlib
import ssl
import subprocess
import unittest
import urllib.error
from pathlib import Path
from typing import Dict, List, Tuple
from unittest import mock

from ota_store import doctor
from ota_store.config import App, Config, load_config, save_config
from ota_store.doctor import Doctor, report, serve_urls
from tests.helpers import temp_store

SERVE_STATUS = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {"mac.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}},
    "Services": {"svc:apps": {"Web": {"apps.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}}}},
}


class Commands:
    """A command runner answering from a table keyed by the first two words."""

    def __init__(self, answers: Dict[str, Tuple[int, str]]):
        self.answers = answers
        self.calls: List[List[str]] = []

    def __call__(self, arguments: List[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(arguments)
        key = " ".join(arguments[:2])
        if key not in self.answers:
            raise FileNotFoundError(arguments[0])
        code, output = self.answers[key]
        return subprocess.CompletedProcess(arguments, code, output, "" if code == 0 else output)


class ServeStatusTests(unittest.TestCase):
    def test_urls_proxying_to_the_port_services_first(self):
        self.assertEqual(serve_urls(SERVE_STATUS, 8765), ["https://apps.example.ts.net", "https://mac.example.ts.net"])
        self.assertEqual(serve_urls(SERVE_STATUS, 9000), [])
        self.assertEqual(serve_urls({}, 8765), [])

    def test_proxy_forms_and_other_ports(self):
        status = {"Web": {
            "a.ts.net:443": {"Handlers": {"/": {"Proxy": "localhost:8765"}}},
            "b.ts.net:8443": {"Handlers": {"/": {"Proxy": "8765"}}},
            "c.ts.net:443": {"Handlers": {"/": {"Proxy": "http://10.0.0.2:8765"}}},
            "d.ts.net:443": {"Handlers": {"/docs": {"Proxy": "http://127.0.0.1:8765"}}},
        }}
        self.assertEqual(serve_urls(status, 8765), ["https://a.ts.net", "https://b.ts.net:8443"])

    def test_tailscale_urls_survive_failures(self):
        ok = Commands({"tailscale serve": (0, json.dumps(SERVE_STATUS))})
        self.assertEqual(doctor.tailscale_urls(8765, ok)[0], "https://apps.example.ts.net")
        self.assertEqual(doctor.tailscale_urls(8765, Commands({})), [])
        self.assertEqual(doctor.tailscale_urls(8765, Commands({"tailscale serve": (1, "not logged in")})), [])
        self.assertEqual(doctor.tailscale_urls(8765, Commands({"tailscale serve": (0, "garbage")})), [])

    def test_public_url_prefers_the_config(self):
        self.assertEqual(doctor.public_url(Config(url="https://x.example"), Commands({})), "https://x.example")


class DoctorTests(unittest.TestCase):
    def setUp(self):
        context = temp_store()
        self.store = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.xcode = self.store / "Xcode.app" / "Contents"
        (self.xcode / "Developer").mkdir(parents=True)
        with open(self.xcode / "version.plist", "wb") as file:
            plistlib.dump({"CFBundleShortVersionString": "26.1", "ProductBuildVersion": "17B55"}, file)
        self.commands = Commands({
            "xcode-select -p": (0, f"{self.xcode / 'Developer'}\n"),
            "security find-identity": (0, '  1) ABC "Apple Development: Jo Doe (TEAM123)"\n     1 valid identities found\n'),
            "launchctl print": (0, "\tstate = running\n\tpid = 7\n"),
            "tailscale serve": (0, json.dumps(SERVE_STATUS)),
        })
        self.pages = {"http://127.0.0.1:8765/api/apps": (200, b'{"title": "Apps", "apps": []}'),
                      "https://apps.example.ts.net/": (200, b"<html>")}

    def fetch(self, url: str, timeout: float) -> Tuple[int, bytes]:
        answer = self.pages.get(url)
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        return answer

    def run_doctor(self, **options):
        options.setdefault("environ", {})
        found = Doctor(run=self.commands, fetch=self.fetch, launch_agents=self.store / "LaunchAgents", **options)
        return list(found.checks()), found

    def pick(self, checks: List[doctor.Check], start: str) -> doctor.Check:
        """The first check whose text starts with `start`."""
        for check in checks:
            if check.text.startswith(start):
                return check
        self.fail(f"No check starting {start!r} in {[check.text for check in checks]}")

    def test_everything_fine(self):
        app_dir = self.store / "src"
        (app_dir / "App.xcodeproj").mkdir(parents=True)
        config = load_config(apply_environment=False)
        config.apps["app"] = App(id="app", name="App", dir=app_dir, project=app_dir / "App.xcodeproj", scheme="App")
        save_config(config)
        checks, found = self.run_doctor()
        failed = [check.text for check in checks if not check.ok]
        self.assertEqual(failed, [])
        self.assertIn("Xcode 26.1 (17B55)", self.pick(checks, "Xcode").text)
        self.assertIn("Jo Doe (TEAM123)", self.pick(checks, "Apple").text)
        self.assertIn("1 app", self.pick(checks, "Config").text)
        self.assertIn("https://apps.example.ts.net answers over HTTPS", self.pick(checks, "https://apps.example.ts.net").text)
        self.assertTrue(self.pick(checks, "app").ok)
        self.assertIn(["launchctl", "print", f"gui/{__import__('os').getuid()}/local.ota-store"], self.commands.calls)

    def test_command_line_tools_only(self):
        self.commands.answers["xcode-select -p"] = (0, "/Library/Developer/CommandLineTools\n")
        checks, _ = self.run_doctor()
        self.assertFalse(self.pick(checks, "Only").ok)
        self.assertIn("xcode-select -s", self.pick(checks, "Only").fix)

    def test_developer_dir_wins(self):
        checks, _ = self.run_doctor(environ={"DEVELOPER_DIR": str(self.xcode / "Developer")})
        self.assertTrue(self.pick(checks, "Xcode").ok)
        self.assertNotIn(["xcode-select", "-p"], self.commands.calls)

    def test_no_signing_certificate(self):
        self.commands.answers["security find-identity"] = (0, "     0 valid identities found\n")
        checks, _ = self.run_doctor()
        self.assertFalse(self.pick(checks, "No Apple").ok)
        self.assertIn("Accounts", self.pick(checks, "No Apple").fix)

    def test_bad_config_stops_the_rest(self):
        from ota_store.config import config_path
        config_path().parent.mkdir(parents=True)
        config_path().write_text("[1")
        checks, _ = self.run_doctor()
        self.assertFalse(self.pick(checks, "Config").ok)
        self.assertIn("edit", self.pick(checks, "Config").fix)
        self.assertFalse(any(check.text.startswith("Server") for check in checks))

    def test_missing_app_folder(self):
        config = load_config(apply_environment=False)
        config.apps["gone"] = App(id="gone", name="Gone", dir=self.store / "missing")
        config.apps["pub"] = App(id="pub", name="Pub")
        save_config(config)
        checks, _ = self.run_doctor()
        self.assertFalse(self.pick(checks, "gone").ok)
        self.assertIn("ota-store remove gone", self.pick(checks, "gone").fix)
        self.assertTrue(self.pick(checks, "pub").ok)

    def test_no_server_and_an_old_one(self):
        del self.pages["http://127.0.0.1:8765/api/apps"]
        checks, _ = self.run_doctor()
        self.assertFalse(self.pick(checks, "No server").ok)
        self.assertIn("ota-store service install", self.pick(checks, "No server").fix)
        self.pages["http://127.0.0.1:8765/api/apps"] = (404, b"<html>Not Found</html>")
        checks, _ = self.run_doctor()
        self.assertIn("older store", self.pick(checks, "Something").text)

    def test_service_not_installed(self):
        self.commands.answers["launchctl print"] = (113, "Could not find service")
        checks, _ = self.run_doctor()
        self.assertFalse(self.pick(checks, "LaunchAgent").ok)
        self.assertIn("not installed", self.pick(checks, "LaunchAgent").text)

    def test_tailscale_missing_or_not_serving(self):
        checks, _ = self.run_doctor(tailscale="")
        self.assertFalse(self.pick(checks, "Tailscale").ok)
        self.assertFalse(self.pick(checks, "No public URL").ok)
        self.commands.answers["tailscale serve"] = (0, "{}")
        checks, _ = self.run_doctor()
        self.assertIn("tailscale serve --bg --https=443 http://127.0.0.1:8765", self.pick(checks, "tailscale").fix)

    def test_configured_url_without_tailscale(self):
        save_config(Config(url="https://store.example.com"))
        self.pages["https://store.example.com/"] = (200, b"")
        checks, _ = self.run_doctor(tailscale="")
        self.assertTrue(self.pick(checks, "No Tailscale").ok)
        self.assertTrue(self.pick(checks, "https://store.example.com").ok)

    def test_public_url_failures(self):
        self.pages["https://apps.example.ts.net/"] = urllib.error.URLError(ssl.SSLError(1, "CERTIFICATE_VERIFY_FAILED"))
        checks, _ = self.run_doctor()
        self.assertIn("certificate", self.pick(checks, "https://apps.example.ts.net:").text)
        self.pages["https://apps.example.ts.net/"] = urllib.error.URLError(TimeoutError("timed out"))
        checks, _ = self.run_doctor()
        self.assertIn("didn't answer", self.pick(checks, "https://apps.example.ts.net").text)
        save_config(Config(url="http://plain.example"))
        checks, _ = self.run_doctor()
        self.assertIn("isn't HTTPS", self.pick(checks, "http://plain.example").text)

    def test_report_lines(self):
        text = report([doctor.Check(True, "fine"), doctor.Check(False, "broken", "fix it")])
        self.assertEqual(text, "✓ fine\n✗ broken — fix it")
        self.assertIn("\033[31m", report([doctor.Check(False, "broken")], color=True))


if __name__ == "__main__":
    unittest.main()
