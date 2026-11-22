"""The LaunchAgent that keeps `ota-store serve` running while you're logged in.

    ~/Library/LaunchAgents/local.ota-store.plist   runs the launcher's `serve`
    home()/logs/server.log                         its stdout and stderr

It's driven with `launchctl bootstrap|bootout gui/<uid>`, `kickstart -k` and
`print`. The LaunchAgents folder and the command runner are parameters, so
tests never touch the real ones.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config

LABEL = "local.ota-store"
LAUNCHER = Path(__file__).resolve().parent.parent / "ota-store"
# The Python that ships with Xcode. Homebrew's moves on every upgrade, which
# would leave the LaunchAgent pointing at a path that's gone.
SYSTEM_PYTHON = "/usr/bin/python3"
# LaunchAgents start with only /usr/bin:/bin:/usr/sbin:/sbin; build commands
# run in a login shell, but tools the server itself calls (git) may be in Homebrew.
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Passed on from the environment `service install` runs in, so the service
# uses the same state as the CLI that installed it.
PASSED_ON = ("OTA_STORE_HOME", "OTA_STORE_CACHE")

Runner = Callable[[List[str]], "subprocess.CompletedProcess[str]"]


class ServiceError(Exception):
    """launchctl refused, or the service isn't in a state the command needs. The message says what to do."""


def run_command(arguments: List[str]) -> "subprocess.CompletedProcess[str]":
    """Runs a command and captures its output as text."""
    return subprocess.run(arguments, capture_output=True, text=True, timeout=30)


def launch_agents_dir() -> Path:
    return Path("~/Library/LaunchAgents").expanduser()


def python_path() -> str:
    return SYSTEM_PYTHON if os.path.exists(SYSTEM_PYTHON) else sys.executable


def plist_contents(launcher: Path = LAUNCHER, python: Optional[str] = None) -> Dict[str, Any]:
    """The LaunchAgent's property list, as a dictionary."""
    log = str(config.server_log_path())
    environment = {"PATH": PATH, "LANG": os.environ.get("LANG") or "en_US.UTF-8"}
    for key in PASSED_ON:
        if os.environ.get(key):
            environment[key] = os.environ[key]
    return {
        "Label": LABEL,
        "ProgramArguments": [python or python_path(), str(launcher), "serve"],
        "EnvironmentVariables": environment,
        "WorkingDirectory": str(config.home()),
        "RunAtLoad": True,
        "KeepAlive": True,
        # Builds are what someone is waiting for on their phone; don't let
        # macOS throttle them as background work.
        "ProcessType": "Interactive",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


@dataclass
class Status:
    """What launchd says about the service."""

    installed: bool                 # the plist exists
    loaded: bool                    # launchd knows the service
    state: Optional[str] = None     # "running", "not running", …
    pid: Optional[int] = None
    last_exit: Optional[str] = None

    def describe(self) -> str:
        if not self.installed and not self.loaded:
            return "not installed"
        if not self.loaded:
            return "installed, not loaded"
        if self.pid:
            return f"running (pid {self.pid})"
        text = self.state or "loaded"
        if self.last_exit and self.last_exit != "0":
            text += f", last exit {self.last_exit}"
        return text


def parse_print(output: str) -> Status:
    """A `launchctl print` answer as a Status. Only top-level keys count: nested ones are indented further."""
    status = Status(installed=False, loaded=True)
    for line in output.splitlines():
        if not line.startswith("\t") or line.startswith("\t\t") or " = " not in line:
            continue
        key, _, value = line.strip().partition(" = ")
        if key == "state":
            status.state = value
        elif key == "pid" and value.isdigit():
            status.pid = int(value)
        elif key == "last exit code":
            status.last_exit = value
    return status


class Service:
    """The LaunchAgent, in `folder` (normally ~/Library/LaunchAgents), driven through `run`."""

    def __init__(self, folder: Optional[Path] = None, run: Optional[Runner] = None,
                 uid: Optional[int] = None, launcher: Path = LAUNCHER, python: Optional[str] = None):
        self.folder = Path(folder) if folder is not None else launch_agents_dir()
        self.run = run or run_command
        self.uid = os.getuid() if uid is None else uid
        self.launcher = launcher
        self.python = python

    @property
    def plist_path(self) -> Path:
        return self.folder / f"{LABEL}.plist"

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{LABEL}"

    def _launchctl(self, *arguments: str) -> "subprocess.CompletedProcess[str]":
        try:
            return self.run(["launchctl", *arguments])
        except FileNotFoundError as error:
            raise ServiceError("launchctl isn't on this system; the service needs macOS") from error

    def _check(self, result: "subprocess.CompletedProcess[str]", doing: str) -> None:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            reason = detail[-1] if detail else f"exit code {result.returncode}"
            raise ServiceError(f"launchctl couldn't {doing}: {reason}. "
                               f"See {config.server_log_path()}, or try `ota-store service status`")

    def status(self) -> Status:
        result = self._launchctl("print", self.target)
        if result.returncode != 0:
            status = Status(installed=False, loaded=False)
        else:
            status = parse_print(result.stdout)
        status.installed = self.plist_path.is_file()
        return status

    def write_plist(self) -> Path:
        if not self.launcher.is_file():
            raise ServiceError(f"The launcher isn't at {self.launcher}; reinstall ota-store from a full clone")
        config.server_log_path().parent.mkdir(parents=True, exist_ok=True)
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.plist_path, "wb") as file:
            plistlib.dump(plist_contents(self.launcher, self.python), file)
        return self.plist_path

    def install(self) -> Status:
        """Writes the plist and (re)loads it, which starts the server."""
        self.write_plist()
        if self.status().loaded:
            self._launchctl("bootout", self.target)
        self._check(self._launchctl("bootstrap", self.domain, str(self.plist_path)), "load the service")
        return self.status()

    def uninstall(self) -> None:
        """Stops the server and removes the plist."""
        if self.status().loaded:
            self._check(self._launchctl("bootout", self.target), "unload the service")
        try:
            self.plist_path.unlink()
        except FileNotFoundError:
            pass

    def start(self) -> Status:
        status = self.status()
        if not status.installed:
            raise ServiceError("The service isn't installed. Install it: ota-store service install")
        if status.loaded:
            self._check(self._launchctl("kickstart", self.target), "start the service")
        else:
            self._check(self._launchctl("bootstrap", self.domain, str(self.plist_path)), "load the service")
        return self.status()

    def stop(self) -> None:
        """Unloads the service until the next login or `start`: with KeepAlive, a killed server comes back."""
        if self.status().loaded:
            self._check(self._launchctl("bootout", self.target), "stop the service")

    def restart(self) -> Status:
        status = self.status()
        if not status.loaded:
            return self.start()
        self._check(self._launchctl("kickstart", "-k", self.target), "restart the service")
        return self.status()
