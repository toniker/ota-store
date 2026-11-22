"""The built-in build command: builds an Xcode project for device and writes it to $OTA_OUTPUT.

It's a Debug `xcodebuild build` for any iOS device into the app's own warm
DerivedData, signed the way the project signs (automatic signing with the
development team gives a development profile), then the `.app` zipped into an
`.ipa`. No archive and no export, which is what makes a rebuild take seconds.

    $OTA_PROJECT        the .xcworkspace or .xcodeproj; else the one in the current folder
    $OTA_SCHEME         the scheme; else the only one, or the one named like the project
    $OTA_CONFIGURATION  Debug unless set
    $OTA_DERIVED_DATA   where xcodebuild keeps its build products and caches
    $OTA_OUTPUT         where the .ipa goes

Lines starting with `==> ` name the step it's on; everything else is
xcodebuild's own output, passed through as it comes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional, TextIO

from . import config

# Part of every fingerprint made with this builder: bump it when a change here
# would make a different .ipa from the same sources, so stored builds aren't reused.
BUILDER_VERSION = 1

# Runs a command and returns its standard output; raises BuildError if it fails.
# Tests pass their own, so they don't need Xcode.
Runner = Callable[[List[str]], str]

TARGET_PATTERN = re.compile(r"\(in target '([^']+)' from project '[^']*'\)")
# `xcodebuild -list` resolves packages first, which can take a while on a cold checkout.
LIST_TIMEOUT = 300


class BuildError(Exception):
    """The build can't go ahead or didn't produce an app. Its message says what to do."""


def run(command: List[str]) -> str:
    """The default Runner: runs `command`, returning its stdout."""
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, timeout=LIST_TIMEOUT)
    except FileNotFoundError as error:
        raise BuildError(f"{command[0]} not found. Install Xcode and run `xcode-select -s /Applications/Xcode.app`") from error
    except subprocess.TimeoutExpired as error:
        raise BuildError(f"`{' '.join(command)}` took over {LIST_TIMEOUT} seconds") from error
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip().splitlines()
        raise BuildError(f"`{' '.join(command)}` failed: {detail[-1] if detail else f'exit {result.returncode}'}")
    return result.stdout.decode(errors="replace")


def _kind(project: Path) -> str:
    return "-workspace" if project.suffix == ".xcworkspace" else "-project"


def _names(paths: Iterable[Path]) -> str:
    return ", ".join(path.name for path in paths)


# MARK: - Project and scheme

def find_project(folder: Path) -> Path:
    """The .xcworkspace or .xcodeproj the builder would use in `folder`.

    The one workspace beside the projects (not the one inside every .xcodeproj),
    else the one project.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise BuildError(f"Folder not found: {folder}")
    workspaces = sorted(path for path in folder.glob("*.xcworkspace") if path.is_dir())
    if len(workspaces) == 1:
        return workspaces[0]
    if len(workspaces) > 1:
        raise BuildError(f"{folder} has several workspaces ({_names(workspaces)}). "
                         "Set the app's project to the one to build")
    projects = sorted(path for path in folder.glob("*.xcodeproj") if path.is_dir())
    if len(projects) == 1:
        return projects[0]
    if len(projects) > 1:
        raise BuildError(f"{folder} has several projects ({_names(projects)}). "
                         "Set the app's project to the one to build")
    raise BuildError(f"No .xcworkspace or .xcodeproj in {folder}. Set the app's project to its path, "
                     "or give the app a build command if it isn't built with Xcode")


def list_schemes(project: Path, runner: Runner = run) -> List[str]:
    """The schemes `xcodebuild -list -json` reports for `project`."""
    output = runner(["xcodebuild", "-list", "-json", _kind(project), str(project)])
    # xcodebuild can print warnings before the JSON.
    start = output.find("{")
    try:
        listing = json.loads(output[start:]) if start >= 0 else {}
    except ValueError as error:
        raise BuildError(f"Can't read the schemes of {project.name}: xcodebuild -list said {output[:200]!r}") from error
    entry = listing.get("workspace") or listing.get("project") or {}
    return [str(scheme) for scheme in entry.get("schemes") or []]


def find_scheme(project: Path, preferred: Optional[str] = None, runner: Runner = run) -> str:
    """The scheme to build: `preferred`, else the only one, else the one named like the project."""
    if preferred:
        return preferred
    project = Path(project)
    schemes = list_schemes(project, runner)
    if len(schemes) == 1:
        return schemes[0]
    if not schemes:
        raise BuildError(f"{project.name} has no schemes. Open it in Xcode once so it makes them, "
                         "or share one (Product > Scheme > Manage Schemes, Shared)")
    for scheme in schemes:
        if scheme.lower() == project.stem.lower():
            return scheme
    raise BuildError(f"{project.name} has several schemes: {', '.join(schemes)}. "
                     "Set the app's scheme to the one to build")


# MARK: - Building

def build_command(project: Path, scheme: str, configuration: str, derived_data: Path,
                  update_packages: bool = False) -> List[str]:
    """The xcodebuild command. Without `update_packages`, packages come from the checkouts
    already in DerivedData, which saves asking every remote on each build."""
    command = [
        "xcodebuild", "build", _kind(project), str(project),
        "-scheme", scheme,
        "-configuration", configuration,
        "-destination", "generic/platform=iOS",
        "-derivedDataPath", str(derived_data),
        "-allowProvisioningUpdates",
        "-skipMacroValidation",
        "-skipPackagePluginValidation",
        "-hideShellScriptEnvironment",
        # Indexing is for the editor; the store's builds never open in one.
        "COMPILER_INDEX_STORE_ENABLE=NO",
    ]
    if not update_packages:
        command.insert(-1, "-skipPackageUpdates")
    return command


class Steps:
    """Turns xcodebuild's output into `==> Building <target>` steps as the target changes.

    xcodebuild works on several targets at once and its lines interleave, so a
    new step waits until the last one has shown for `interval` seconds; the
    page would flicker otherwise.
    """

    def __init__(self, interval: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.target: Optional[str] = None
        self.interval = interval
        self.clock = clock
        self.shown_at: Optional[float] = None

    def feed(self, line: str) -> Optional[str]:
        """The step `line` starts, or None if it's still the same one."""
        match = TARGET_PATTERN.search(line)
        if not match or match.group(1) == self.target:
            return None
        now = self.clock()
        if self.shown_at is not None and now - self.shown_at < self.interval:
            return None
        self.target, self.shown_at = match.group(1), now
        return f"==> Building {self.target}"


def stream(command: List[str], out: TextIO = sys.stdout, steps: Optional[Steps] = None) -> int:
    """Runs `command`, copying its output to `out` line by line with the steps added. Its exit code."""
    steps = steps or Steps()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL)
    assert process.stdout is not None
    try:
        for raw in process.stdout:
            line = raw.decode(errors="replace")
            step = steps.feed(line)
            if step:
                out.write(step + "\n")
            out.write(line)
            out.flush()
        return process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise


def products_dir(derived_data: Path, configuration: str) -> Path:
    return Path(derived_data) / "Build" / "Products" / f"{configuration}-iphoneos"


def find_app(products: Path, scheme: str, project: Optional[Path] = None, configuration: str = "Debug",
             runner: Runner = run) -> Path:
    """The .app the build made in `products`: the only one, the one named after the scheme,
    or the app target's FULL_PRODUCT_NAME from `-showBuildSettings -json`."""
    apps = sorted(path for path in Path(products).glob("*.app") if path.is_dir())
    if not apps:
        raise BuildError(f"The build made no .app in {products}. Is {scheme} the scheme of an iOS app?")
    if len(apps) == 1:
        return apps[0]
    for app in apps:
        if app.stem == scheme:
            return app
    if project is not None:
        output = runner(["xcodebuild", "-showBuildSettings", "-json", _kind(project), str(project),
                         "-scheme", scheme, "-configuration", configuration,
                         "-destination", "generic/platform=iOS"])
        start = output.find("[")
        try:
            targets = json.loads(output[start:]) if start >= 0 else []
        except ValueError:
            targets = []
        for target in targets:
            settings = target.get("buildSettings") or {}
            if settings.get("PRODUCT_TYPE") != "com.apple.product-type.application":
                continue
            name = settings.get("FULL_PRODUCT_NAME")
            if name and (Path(products) / name).is_dir():
                return Path(products) / name
    raise BuildError(f"Several apps in {products} ({_names(apps)}) and none is {scheme}.app. "
                     "Delete the stale ones, or set the scheme to the app's")


def package(app: Path, output: Path) -> None:
    """Zips `app` into an .ipa at `output`, which appears complete or not at all."""
    output = Path(output)
    # Beside the output, so the final rename stays on one volume.
    with tempfile.TemporaryDirectory(prefix=".package-", dir=output.parent) as work:
        payload = Path(work) / "Payload"
        payload.mkdir()
        # A clone is instant on APFS; plain -R where cloning isn't possible.
        cloned = subprocess.run(["/bin/cp", "-Rc", str(app), str(payload)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if cloned.returncode != 0:
            shutil.rmtree(payload / app.name, ignore_errors=True)
            subprocess.run(["/bin/cp", "-R", str(app), str(payload)], check=True)
        ipa = Path(work) / "App.ipa"
        # -y keeps the symlinks frameworks rely on; -1 because speed beats a few percent.
        subprocess.run(["/usr/bin/zip", "-qry", "-1", str(ipa), "Payload"], cwd=work, check=True)
        os.replace(ipa, output)


# MARK: - Command

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ota-store xcode-build",
        description="Builds an Xcode project for device (Debug by default) and writes the .ipa to $OTA_OUTPUT. "
                    "Reads OTA_PROJECT, OTA_SCHEME, OTA_CONFIGURATION, OTA_DERIVED_DATA and OTA_OUTPUT.")
    parser.parse_args(argv)

    environment = os.environ
    output_value = environment.get("OTA_OUTPUT")
    if not output_value:
        print("error: OTA_OUTPUT isn't set. It's the path the .ipa should be written to", file=sys.stderr)
        return 2
    output = Path(output_value).expanduser().absolute()
    configuration = environment.get("OTA_CONFIGURATION") or config.DEFAULT_CONFIGURATION
    derived_value = environment.get("OTA_DERIVED_DATA")

    print("==> Preparing", flush=True)
    try:
        project_value = environment.get("OTA_PROJECT")
        project = Path(project_value).expanduser().absolute() if project_value else find_project(Path.cwd())
        if not project.exists():
            raise BuildError(f"Project not found: {project}")
        scheme = find_scheme(project, environment.get("OTA_SCHEME") or None)
        if derived_value:
            derived_data = Path(derived_value).expanduser().absolute()
        else:
            derived_data = config.derived_data(environment.get("OTA_APP_ID") or re.sub(r"[^a-z0-9-]", "-", scheme.lower()))
        derived_data.mkdir(parents=True, exist_ok=True)
        output.parent.mkdir(parents=True, exist_ok=True)

        print(f"Building {scheme} ({configuration}) from {project}", flush=True)
        steps = Steps()
        status = stream(build_command(project, scheme, configuration, derived_data), steps=steps)
        if status != 0 and steps.target is None:
            # It failed before building anything: most likely a package pinned to a
            # version the checkouts don't have yet, which fetching brings in.
            print("The build failed before it started; trying again with package updates", flush=True)
            status = stream(build_command(project, scheme, configuration, derived_data, update_packages=True))
        if status != 0:
            print(f"xcodebuild failed with exit code {status}", flush=True)
            return status

        print("==> Packaging", flush=True)
        app = find_app(products_dir(derived_data, configuration), scheme, project, configuration)
        package(app, output)
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
        return 1
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"error: packaging failed: {error}", file=sys.stderr, flush=True)
        return 1
    print(f"Wrote {output} ({output.stat().st_size / 1e6:.1f} MB) from {app.name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
