"""The LaunchAgent: its plist and the launchctl commands, with a fake folder and runner."""

from __future__ import annotations

import os
import plistlib
import subprocess
import unittest
from pathlib import Path
from typing import List
from unittest import mock

from ota_store import config, service
from ota_store.service import LABEL, Service, ServiceError, parse_print
from tests.helpers import temp_store

PRINT_RUNNING = """gui/501/local.ota-store = {
\tactive count = 1
\tpath = /x/local.ota-store.plist
\tstate = running
\tenvironment = {
\t\tstate = nested
\t}
\tpid = 4242
\tlast exit code = 0
}
"""


class FakeLaunchctl:
    """Records commands and answers like launchctl, tracking whether the service is loaded."""

    def __init__(self, loaded: bool = False, fail: str = ""):
        self.calls: List[List[str]] = []
        self.loaded = loaded
        self.fail = fail

    def __call__(self, arguments: List[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(arguments)
        verb = arguments[1]
        if verb == self.fail:
            return subprocess.CompletedProcess(arguments, 5, "", "Bootstrap failed: 5: Input/output error\n")
        if verb == "print":
            if not self.loaded:
                return subprocess.CompletedProcess(arguments, 113, "", "Could not find service\n")
            return subprocess.CompletedProcess(arguments, 0, PRINT_RUNNING, "")
        if verb == "bootstrap":
            self.loaded = True
        elif verb == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def verbs(self) -> List[str]:
        return [call[1] for call in self.calls if call[1] != "print"]


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.store_context = temp_store()
        self.store = self.store_context.__enter__()
        self.addCleanup(self.store_context.__exit__, None, None, None)
        self.folder = self.store / "LaunchAgents"
        self.launcher = self.store / "ota-store"
        self.launcher.write_text("#!/usr/bin/env python3\n")

    def make(self, launchctl: FakeLaunchctl) -> Service:
        return Service(folder=self.folder, run=launchctl, uid=501, launcher=self.launcher, python="/usr/bin/python3")

    def test_plist_contents(self):
        with mock.patch.dict(os.environ, {"LANG": "en_GB.UTF-8"}):
            contents = service.plist_contents(self.launcher, "/usr/bin/python3")
        self.assertEqual(contents["Label"], "local.ota-store")
        self.assertEqual(contents["ProgramArguments"], ["/usr/bin/python3", str(self.launcher), "serve"])
        self.assertIs(contents["KeepAlive"], True)
        self.assertIs(contents["RunAtLoad"], True)
        self.assertEqual(contents["ProcessType"], "Interactive")
        self.assertEqual(contents["EnvironmentVariables"]["PATH"],
                         "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(contents["EnvironmentVariables"]["LANG"], "en_GB.UTF-8")
        # temp_store sets these, and the service should use the same state.
        self.assertEqual(contents["EnvironmentVariables"]["OTA_STORE_HOME"], os.environ["OTA_STORE_HOME"])
        self.assertEqual(contents["StandardOutPath"], str(config.server_log_path()))
        self.assertEqual(contents["StandardErrorPath"], str(config.server_log_path()))

    def test_python_prefers_the_system_one(self):
        with mock.patch("os.path.exists", return_value=True):
            self.assertEqual(service.python_path(), "/usr/bin/python3")
        with mock.patch("os.path.exists", return_value=False):
            self.assertEqual(service.python_path(), service.sys.executable)

    def test_the_real_launcher_is_next_to_the_package(self):
        self.assertEqual(service.LAUNCHER, Path(__file__).resolve().parent.parent / "ota-store")

    def test_install_writes_the_plist_and_bootstraps(self):
        launchctl = FakeLaunchctl()
        status = self.make(launchctl).install()
        plist = self.folder / "local.ota-store.plist"
        with open(plist, "rb") as file:
            self.assertEqual(plistlib.load(file)["Label"], LABEL)
        self.assertIn(["launchctl", "bootstrap", "gui/501", str(plist)], launchctl.calls)
        self.assertEqual(launchctl.verbs(), ["bootstrap"])
        self.assertTrue(config.server_log_path().parent.is_dir())
        self.assertEqual((status.installed, status.pid), (True, 4242))

    def test_reinstall_boots_out_first(self):
        launchctl = FakeLaunchctl(loaded=True)
        self.make(launchctl).install()
        self.assertEqual(launchctl.verbs(), ["bootout", "bootstrap"])
        self.assertIn(["launchctl", "bootout", "gui/501/local.ota-store"], launchctl.calls)

    def test_uninstall(self):
        launchctl = FakeLaunchctl()
        agent = self.make(launchctl)
        agent.install()
        agent.uninstall()
        self.assertFalse(agent.plist_path.exists())
        self.assertEqual(launchctl.verbs(), ["bootstrap", "bootout"])
        agent.uninstall()

    def test_start_stop_restart(self):
        launchctl = FakeLaunchctl()
        agent = self.make(launchctl)
        with self.assertRaises(ServiceError) as caught:
            agent.start()
        self.assertIn("ota-store service install", str(caught.exception))
        agent.write_plist()
        agent.start()
        agent.start()
        agent.restart()
        agent.stop()
        self.assertEqual(launchctl.verbs(), ["bootstrap", "kickstart", "kickstart", "bootout"])
        self.assertIn(["launchctl", "kickstart", "-k", "gui/501/local.ota-store"], launchctl.calls)

    def test_failures_say_what_to_do(self):
        launchctl = FakeLaunchctl(fail="bootstrap")
        with self.assertRaises(ServiceError) as caught:
            self.make(launchctl).install()
        self.assertIn("Input/output error", str(caught.exception))
        self.assertIn("ota-store service status", str(caught.exception))

    def test_missing_launcher(self):
        agent = Service(folder=self.folder, run=FakeLaunchctl(), uid=501, launcher=self.store / "nope")
        with self.assertRaises(ServiceError):
            agent.install()
        self.assertFalse(self.folder.exists())

    def test_status(self):
        agent = self.make(FakeLaunchctl())
        self.assertEqual(agent.status().describe(), "not installed")
        agent.write_plist()
        self.assertEqual(agent.status().describe(), "installed, not loaded")

    def test_parse_print_reads_only_top_level_keys(self):
        status = parse_print(PRINT_RUNNING)
        self.assertEqual((status.state, status.pid, status.last_exit), ("running", 4242, "0"))
        stopped = parse_print("x = {\n\tstate = not running\n\tlast exit code = 78: EX_CONFIG\n}\n")
        stopped.installed = True
        self.assertEqual(stopped.describe(), "not running, last exit 78: EX_CONFIG")

    def test_default_folder_is_the_users(self):
        # Only computes the path; nothing is written there.
        self.assertEqual(Service(run=FakeLaunchctl()).folder, Path.home() / "Library" / "LaunchAgents")


if __name__ == "__main__":
    unittest.main()
