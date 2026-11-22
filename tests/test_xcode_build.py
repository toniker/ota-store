"""Tests for the built-in Xcode builder. The ones that run xcodebuild need OTA_TEST_XCODE=1."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path
from typing import List

from ota_store import xcode_build
from ota_store.xcode_build import BuildError, Steps, find_app, find_project, find_scheme, package


def listing(kind: str, name: str, schemes: List[str]):
    """A Runner answering `xcodebuild -list -json` with `schemes`, recording the commands it got."""
    calls: List[List[str]] = []

    def runner(command: List[str]) -> str:
        calls.append(command)
        return "2026-11-21 note: some warning\n" + json.dumps({kind: {"name": name, "schemes": schemes}})
    runner.calls = calls  # type: ignore[attr-defined]
    return runner


class FindProjectTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.folder)]))

    def make(self, *names: str) -> None:
        for name in names:
            (self.folder / name).mkdir(parents=True)

    def test_project(self):
        self.make("App.xcodeproj/project.xcworkspace")
        self.assertEqual(find_project(self.folder), self.folder / "App.xcodeproj")

    def test_workspace_beats_project(self):
        self.make("App.xcodeproj/project.xcworkspace", "App.xcworkspace")
        self.assertEqual(find_project(self.folder), self.folder / "App.xcworkspace")

    def test_several_projects_are_named(self):
        self.make("A.xcodeproj", "B.xcodeproj")
        with self.assertRaises(BuildError) as raised:
            find_project(self.folder)
        self.assertIn("A.xcodeproj, B.xcodeproj", str(raised.exception))

    def test_several_workspaces_are_named(self):
        self.make("A.xcworkspace", "B.xcworkspace", "A.xcodeproj")
        with self.assertRaisesRegex(BuildError, "A.xcworkspace, B.xcworkspace"):
            find_project(self.folder)

    def test_nothing(self):
        self.make("Sources")
        with self.assertRaisesRegex(BuildError, "No .xcworkspace or .xcodeproj"):
            find_project(self.folder)

    def test_missing_folder(self):
        with self.assertRaisesRegex(BuildError, "not found"):
            find_project(self.folder / "gone")


class FindSchemeTests(unittest.TestCase):
    project = Path("/work/App.xcodeproj")

    def test_preferred_needs_no_listing(self):
        def runner(command):
            raise AssertionError("listed schemes")
        self.assertEqual(find_scheme(self.project, "Other", runner=runner), "Other")

    def test_only_scheme(self):
        runner = listing("project", "App", ["Something"])
        self.assertEqual(find_scheme(self.project, runner=runner), "Something")
        self.assertEqual(runner.calls, [["xcodebuild", "-list", "-json", "-project", str(self.project)]])

    def test_named_like_project(self):
        runner = listing("project", "App", ["AppWidgets", "App", "AppTests"])
        self.assertEqual(find_scheme(self.project, runner=runner), "App")

    def test_workspace(self):
        runner = listing("workspace", "App", ["Pods-App", "app"])
        workspace = Path("/work/App.xcworkspace")
        self.assertEqual(find_scheme(workspace, runner=runner), "app")
        self.assertEqual(runner.calls[0][3:], ["-workspace", str(workspace)])

    def test_ambiguous_lists_the_schemes(self):
        runner = listing("project", "App", ["Alpha", "Beta"])
        with self.assertRaises(BuildError) as raised:
            find_scheme(self.project, runner=runner)
        self.assertIn("Alpha, Beta", str(raised.exception))
        self.assertIn("scheme", str(raised.exception))

    def test_no_schemes(self):
        with self.assertRaisesRegex(BuildError, "no schemes"):
            find_scheme(self.project, runner=listing("project", "App", []))

    def test_unreadable_listing(self):
        with self.assertRaises(BuildError):
            find_scheme(self.project, runner=lambda command: "{not json")


class StepsTests(unittest.TestCase):
    LINES = [
        "Prepare packages\n",
        "ComputeTargetDependencyGraph\n",
        "SwiftCompile normal arm64 /x/A.swift (in target 'Core' from project 'App')\n",
        "    cd /x\n",
        "SwiftCompile normal arm64 /x/B.swift (in target 'Core' from project 'App')\n",
        "Ld /x/App (in target 'App' from project 'App')\n",
        "CodeSign /x/App.app (in target 'App' from project 'App')\n",
    ]

    def test_steps_as_targets_change(self):
        steps = Steps(interval=0)
        self.assertEqual([step for step in map(steps.feed, self.LINES) if step],
                         ["==> Building Core", "==> Building App"])

    def test_quick_changes_wait(self):
        now = [0.0]
        steps = Steps(interval=1.0, clock=lambda: now[0])
        self.assertEqual(steps.feed("x (in target 'A' from project 'P')"), "==> Building A")
        now[0] = 0.5
        self.assertIsNone(steps.feed("x (in target 'B' from project 'P')"))
        now[0] = 1.5
        self.assertEqual(steps.feed("x (in target 'B' from project 'P')"), "==> Building B")

    def test_stream_passes_output_through(self):
        script = "import sys\nprint('hello')\nprint(\"Ld x (in target 'App' from project 'App')\")\nsys.exit(65)\n"
        out = io.StringIO()
        status = xcode_build.stream([sys.executable, "-c", script], out)
        self.assertEqual(status, 65)
        self.assertEqual(out.getvalue(),
                         "hello\n==> Building App\nLd x (in target 'App' from project 'App')\n")


class FindAppTests(unittest.TestCase):
    def setUp(self):
        self.products = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.products)]))

    def test_only_app(self):
        (self.products / "Thing.app").mkdir()
        (self.products / "Core.framework").mkdir()
        self.assertEqual(find_app(self.products, "Scheme"), self.products / "Thing.app")

    def test_named_after_scheme(self):
        (self.products / "Old.app").mkdir()
        (self.products / "App.app").mkdir()
        self.assertEqual(find_app(self.products, "App"), self.products / "App.app")

    def test_asks_build_settings(self):
        (self.products / "Old.app").mkdir()
        (self.products / "My App.app").mkdir()
        settings = [
            {"target": "Widgets", "buildSettings": {"PRODUCT_TYPE": "com.apple.product-type.app-extension",
                                                    "FULL_PRODUCT_NAME": "Widgets.appex"}},
            {"target": "App", "buildSettings": {"PRODUCT_TYPE": "com.apple.product-type.application",
                                                "FULL_PRODUCT_NAME": "My App.app"}},
        ]
        calls = []

        def runner(command):
            calls.append(command)
            return "Command line invocation:\n" + json.dumps(settings)
        app = find_app(self.products, "App", Path("/w/App.xcodeproj"), "Debug", runner)
        self.assertEqual(app, self.products / "My App.app")
        self.assertIn("-showBuildSettings", calls[0])

    def test_no_app(self):
        with self.assertRaisesRegex(BuildError, "no .app"):
            find_app(self.products, "App")

    def test_undecidable(self):
        (self.products / "A.app").mkdir()
        (self.products / "B.app").mkdir()
        with self.assertRaisesRegex(BuildError, "A.app, B.app"):
            find_app(self.products, "App")


class PackageTests(unittest.TestCase):
    def test_payload_with_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = root / "Build" / "App.app"
            framework = app / "Frameworks" / "Core.framework"
            (framework / "Versions" / "A").mkdir(parents=True)
            (framework / "Versions" / "A" / "Core").write_bytes(b"binary")
            os.symlink("Versions/A/Core", framework / "Core")
            (app / "Info.plist").write_bytes(b"plist")
            (app / "App").write_bytes(b"executable")
            output = root / "out" / "App.ipa"
            output.parent.mkdir()

            package(app, output)

            self.assertEqual(sorted(path.name for path in output.parent.iterdir()), ["App.ipa"])
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertIn("Payload/App.app/Info.plist", names)
                self.assertIn("Payload/App.app/App", names)
                self.assertTrue(all(name.startswith("Payload/") for name in names))
                link = archive.getinfo("Payload/App.app/Frameworks/Core.framework/Core")
                self.assertEqual((link.external_attr >> 16) & 0o170000, 0o120000)
                self.assertEqual(archive.read(link), b"Versions/A/Core")
            # The original is untouched.
            self.assertTrue(os.path.islink(framework / "Core"))


class MainTests(unittest.TestCase):
    def test_needs_output(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"OTA_OUTPUT": ""}), mock.patch("sys.stderr", stderr):
            self.assertNotEqual(xcode_build.main([]), 0)
        self.assertIn("OTA_OUTPUT", stderr.getvalue())

    def test_command(self):
        command = xcode_build.build_command(Path("/w/App.xcworkspace"), "App", "Debug", Path("/dd"))
        self.assertEqual(command[:4], ["xcodebuild", "build", "-workspace", "/w/App.xcworkspace"])
        for flag in ("-allowProvisioningUpdates", "generic/platform=iOS", "COMPILER_INDEX_STORE_ENABLE=NO"):
            self.assertIn(flag, command)
        self.assertIn("-skipPackageUpdates", command)
        self.assertNotIn("-skipPackageUpdates", xcode_build.build_command(
            Path("/w/App.xcodeproj"), "App", "Debug", Path("/dd"), update_packages=True))

    def run_main(self, attempts):
        """main() with xcodebuild replaced: each attempt is (lines it prints, exit code)."""
        commands = []

        def fake_stream(command, out=sys.stdout, steps=None):
            commands.append(command)
            lines, status = attempts[len(commands) - 1]
            for line in lines:
                if steps is not None:
                    steps.feed(line)
            return status

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "App.xcodeproj").mkdir()
            app = root / "dd" / "Build" / "Products" / "Debug-iphoneos" / "App.app"
            app.mkdir(parents=True)
            (app / "Info.plist").write_bytes(b"plist")
            environment = {"OTA_OUTPUT": str(root / "out" / "App.ipa"), "OTA_PROJECT": str(root / "App.xcodeproj"),
                           "OTA_SCHEME": "App", "OTA_CONFIGURATION": "", "OTA_DERIVED_DATA": str(root / "dd")}
            with mock.patch.dict(os.environ, environment), mock.patch.object(xcode_build, "stream", fake_stream), \
                    mock.patch("sys.stdout", io.StringIO()) as out, mock.patch("sys.stderr", io.StringIO()):
                status = xcode_build.main([])
            made = (root / "out" / "App.ipa").is_file()
        return status, commands, made, out.getvalue()

    def test_builds_and_packages(self):
        status, commands, made, out = self.run_main([(["Ld x (in target 'App' from project 'App')"], 0)])
        self.assertEqual((status, len(commands), made), (0, 1, True))
        self.assertTrue(out.startswith("==> Preparing\n"))
        self.assertIn("==> Packaging\n", out)

    def test_early_failure_retries_with_package_updates(self):
        status, commands, made, _ = self.run_main([(["error: Could not resolve package dependencies"], 74), ([], 0)])
        self.assertEqual((status, made), (0, True))
        self.assertIn("-skipPackageUpdates", commands[0])
        self.assertNotIn("-skipPackageUpdates", commands[1])

    def test_compile_failure_returns_its_code(self):
        status, commands, made, _ = self.run_main([(["CompileSwift x (in target 'App' from project 'App')"], 65)])
        self.assertEqual((status, len(commands), made), (65, 1, False))


@unittest.skipUnless(os.environ.get("OTA_TEST_XCODE"), "set OTA_TEST_XCODE=1 to run xcodebuild")
class XcodeTests(unittest.TestCase):
    def test_list_schemes_of_a_real_project(self):
        project = os.environ.get("OTA_TEST_PROJECT")
        if not project:
            self.skipTest("set OTA_TEST_PROJECT to an .xcodeproj to list its schemes")
        self.assertTrue(xcode_build.list_schemes(Path(project)))


if __name__ == "__main__":
    unittest.main()
