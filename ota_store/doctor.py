"""`ota-store doctor`: read-only checks of everything a phone install depends on.

Each check is one line, ✓ or ✗, and a failed one says how to fix it. The
command runner and the HTTP fetcher are parameters, so tests don't need
Xcode, Tailscale or a network.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

from . import config as config_module

Runner = Callable[[List[str]], "subprocess.CompletedProcess[str]"]
# (status, body) for a GET of the URL; raises OSError (URLError included) if nothing answers.
Fetcher = Callable[[str, float], Tuple[int, bytes]]

TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
PUBLIC_TIMEOUT = 5.0
LOCAL_TIMEOUT = 2.0


@dataclass
class Check:
    """One line of the report."""

    ok: bool
    text: str
    fix: Optional[str] = None


def run_command(arguments: List[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(arguments, capture_output=True, text=True, timeout=15)


def http_get(url: str, timeout: float) -> Tuple[int, bytes]:
    """GETs `url`. HTTP errors are answers too, so they return their status instead of raising."""
    request = urllib.request.Request(url, headers={"User-Agent": "ota-store-doctor"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(1_000_000)
    except urllib.error.HTTPError as error:
        return error.code, error.read(1_000_000)


# MARK: - Tailscale

def tailscale_binary() -> Optional[str]:
    found = shutil.which("tailscale")
    if found:
        return found
    return TAILSCALE_APP if os.path.exists(TAILSCALE_APP) else None


def _proxy_port(proxy: str) -> Optional[int]:
    """The port of a `tailscale serve` proxy target: "http://127.0.0.1:8765", "localhost:8765" or "8765"."""
    if proxy.isdigit():
        return int(proxy)
    if "://" not in proxy:
        proxy = "http://" + proxy
    try:
        parts = urlsplit(proxy)
        if parts.hostname not in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return None
        return parts.port
    except ValueError:
        return None


def serve_urls(status: Dict[str, Any], port: int) -> List[str]:
    """The HTTPS URLs in `tailscale serve status --json` that proxy `/` to `port`, Services first."""
    webs: List[Dict[str, Any]] = []
    for service in (status.get("Services") or {}).values():
        webs.append((service or {}).get("Web") or {})
    webs.append(status.get("Web") or {})
    urls: List[str] = []
    for web in webs:
        for host_port, entry in web.items():
            handler = ((entry or {}).get("Handlers") or {}).get("/") or {}
            if _proxy_port(str(handler.get("Proxy") or "")) != port:
                continue
            host, _, public_port = host_port.rpartition(":")
            url = f"https://{host}" if public_port == "443" else f"https://{host_port}"
            if url not in urls:
                urls.append(url)
    return urls


def tailscale_urls(port: int, run: Optional[Runner] = None) -> List[str]:
    """The URLs `tailscale serve` puts the store at, or [] if there's no Tailscale or nothing serves it."""
    binary = tailscale_binary() if run is None else "tailscale"
    if binary is None:
        return []
    try:
        result = (run or run_command)([binary, "serve", "status", "--json"])
        return serve_urls(json.loads(result.stdout or "{}"), port) if result.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return []


def public_url(config: config_module.Config, run: Optional[Runner] = None) -> Optional[str]:
    """The URL phones open: `url` from the config, else the first one `tailscale serve` provides."""
    if config.url:
        return config.url
    urls = tailscale_urls(config.port, run)
    return urls[0] if urls else None


# MARK: - Checks

def local_address(config: config_module.Config) -> str:
    host = config.host if config.host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    return f"{host}:{config.port}"


class Doctor:
    """Runs the checks. `run` and `fetch` default to real commands and real HTTP."""

    def __init__(self, run: Optional[Runner] = None, fetch: Optional[Fetcher] = None,
                 tailscale: Optional[str] = None, environ: Optional[Dict[str, str]] = None,
                 launch_agents: Optional[Path] = None):
        self.run = run or run_command
        self.fetch = fetch or http_get
        self.environ = os.environ if environ is None else environ
        # With an injected runner, assume a `tailscale` on PATH unless told otherwise.
        self.tailscale = tailscale if tailscale is not None else (
            (tailscale_binary() or "") if run is None else "tailscale")
        self.launch_agents = launch_agents
        self.config: Optional[config_module.Config] = None
        self.serve_urls: List[str] = []

    def _run(self, arguments: List[str]) -> Optional["subprocess.CompletedProcess[str]"]:
        try:
            return self.run(arguments)
        except (OSError, subprocess.SubprocessError):
            return None

    def checks(self) -> Iterator[Check]:
        """Every check, in order, as each finishes. Later ones use what earlier ones found."""
        yield self.python()
        yield from self.xcode()
        yield self.signing()
        yield self.load_config()
        if self.config is not None:
            yield from self.apps()
            yield self.service()
            yield self.server()
            yield self.tailscale_serve()
            yield self.public()

    def python(self) -> Check:
        version = ".".join(str(part) for part in sys.version_info[:3])
        if sys.version_info < (3, 9):
            return Check(False, f"Python {version} is too old", "run ota-store with /usr/bin/python3 (3.9 or later)")
        return Check(True, f"Python {version} ({sys.executable})")

    def xcode(self) -> List[Check]:
        developer = self.environ.get("DEVELOPER_DIR")
        if not developer:
            result = self._run(["xcode-select", "-p"])
            developer = result.stdout.strip() if result is not None and result.returncode == 0 else ""
        fix = "install Xcode from the App Store, then: sudo xcode-select -s /Applications/Xcode.app"
        if not developer:
            return [Check(False, "No Xcode selected", fix)]
        if "CommandLineTools" in developer:
            return [Check(False, f"Only the Command Line Tools are selected ({developer})", fix)]
        try:
            with open(Path(developer).parent / "version.plist", "rb") as file:
                info = plistlib.load(file)
            version = f"{info.get('CFBundleShortVersionString', '?')} ({info.get('ProductBuildVersion', '?')})"
        except (OSError, ValueError, plistlib.InvalidFileException):
            return [Check(False, f"Can't read the Xcode version at {developer}", fix)]
        return [Check(True, f"Xcode {version} at {Path(developer).parent.parent}")]

    def signing(self) -> Check:
        result = self._run(["security", "find-identity", "-v", "-p", "codesigning"])
        fix = ("in Xcode → Settings → Accounts, sign in to the team's Apple account; "
               "a first build with automatic signing makes the certificate")
        if result is None or result.returncode != 0:
            return Check(False, "Can't list signing identities", fix)
        identities = [line for line in result.stdout.splitlines() if "Apple Development" in line]
        if not identities:
            return Check(False, "No Apple Development signing certificate in the keychain", fix)
        names = sorted({match.group(1) for line in identities
                        for match in [re.search(r'"Apple Development: ([^"]*)"', line)] if match})
        return Check(True, f"Apple Development certificate: {', '.join(names) or len(identities)}")

    def load_config(self) -> Check:
        path = config_module.config_path()
        try:
            self.config = config_module.load_config()
        except config_module.ConfigError as error:
            return Check(False, f"Config: {error}", f"edit {path}")
        except ValueError as error:
            return Check(False, f"Config: {error}", f"edit {path}")
        count = len(self.config.apps)
        if not path.exists():
            return Check(True, f"No config yet at {path}; register an app: ota-store add DIR")
        return Check(True, f"Config {path}: {count} app{'' if count == 1 else 's'}")

    def apps(self) -> List[Check]:
        results = []
        assert self.config is not None
        for app in self.config.apps.values():
            if app.dir is None:
                results.append(Check(True, f"{app.id}: published builds only"))
            elif not app.dir.is_dir():
                results.append(Check(False, f"{app.id}: folder not found: {app.dir}",
                                     f"fix dir in {config_module.config_path()}, or: ota-store remove {app.id}"))
            elif app.project is not None and not app.project.exists():
                results.append(Check(False, f"{app.id}: project not found: {app.project}",
                                     f"fix project in {config_module.config_path()}"))
            else:
                what = f"command `{app.command}`" if app.command else (app.scheme or "built-in builder")
                results.append(Check(True, f"{app.id}: {app.dir} ({what})"))
        return results

    def service(self) -> Check:
        from .service import LABEL, Service, ServiceError

        try:
            status = Service(folder=self.launch_agents, run=self.run).status()
        except (ServiceError, OSError, subprocess.SubprocessError) as error:
            return Check(False, f"Can't ask launchd about {LABEL}: {error}", "run ota-store service status")
        if status.pid:
            return Check(True, f"LaunchAgent {LABEL}: {status.describe()}")
        return Check(False, f"LaunchAgent {LABEL}: {status.describe()}",
                     f"ota-store service install, then see {config_module.server_log_path()}"
                     if status.installed else "ota-store service install")

    def server(self) -> Check:
        assert self.config is not None
        address = local_address(self.config)
        try:
            status, body = self.fetch(f"http://{address}/api/apps", LOCAL_TIMEOUT)
        except (OSError, ValueError):
            return Check(False, f"No server answers on {address}",
                         "start it: ota-store service install (or ota-store serve to try it)")
        try:
            answer = json.loads(body)
            ours = status == 200 and isinstance(answer, dict) and "apps" in answer
        except ValueError:
            ours = False
        if not ours:
            return Check(False, f"Something on {address} answers, but not as ota-store (an older store?)",
                         "stop it, or set another port in the config and reinstall: ota-store service install")
        return Check(True, f"Server answers on {address}")

    def tailscale_serve(self) -> Check:
        assert self.config is not None
        port = self.config.port
        setup = f"tailscale serve --bg --https=443 http://127.0.0.1:{port}"
        if not self.tailscale:
            if self.config.url:
                return Check(True, f"No Tailscale; using url {self.config.url} from the config")
            return Check(False, "Tailscale isn't installed",
                         "install it from https://tailscale.com/download, or put an HTTPS proxy in front "
                         f"of 127.0.0.1:{port} and set url in {config_module.config_path()}")
        result = self._run([self.tailscale, "serve", "status", "--json"])
        if result is None or result.returncode != 0:
            detail = (result.stderr.strip().splitlines() or [""])[-1] if result is not None else ""
            return Check(False, f"`tailscale serve status` failed{': ' + detail if detail else ''}",
                         "open Tailscale and log in, then: " + setup)
        try:
            self.serve_urls = serve_urls(json.loads(result.stdout or "{}"), port)
        except ValueError:
            return Check(False, "Can't read `tailscale serve status --json`", setup)
        if not self.serve_urls:
            if self.config.url:
                return Check(True, f"tailscale serve doesn't proxy port {port}; using url {self.config.url}")
            return Check(False, f"tailscale serve doesn't proxy to port {port}", setup)
        return Check(True, f"tailscale serve: {', '.join(self.serve_urls)} → 127.0.0.1:{port}")

    def public(self) -> Check:
        assert self.config is not None
        url = self.config.url or (self.serve_urls[0] if self.serve_urls else None)
        if url is None:
            return Check(False, "No public URL",
                         f"run tailscale serve (above), or set url in {config_module.config_path()}")
        if not url.startswith("https://"):
            return Check(False, f"{url} isn't HTTPS; iOS installs only over HTTPS",
                         "put an HTTPS proxy with a trusted certificate in front of the store")
        try:
            status, _ = self.fetch(url + "/", PUBLIC_TIMEOUT)
        except ssl.SSLError as error:
            return Check(False, f"{url}: certificate not trusted ({getattr(error, 'reason', None) or error})",
                         "use a certificate iOS trusts; tailscale serve provides one")
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLError):
                reason = getattr(error.reason, "reason", None) or error.reason
                return Check(False, f"{url}: certificate not trusted ({reason})",
                             "use a certificate iOS trusts; tailscale serve provides one")
            return Check(False, f"{url} didn't answer ({error.reason})",
                         "check this Mac is connected to the tailnet, and that the proxy is running")
        except (OSError, ValueError) as error:
            return Check(False, f"{url} didn't answer within {PUBLIC_TIMEOUT:g} s ({error})",
                         "check this Mac is connected to the tailnet, and that the proxy is running")
        if status != 200:
            return Check(False, f"{url} answered {status}",
                         f"point the proxy at http://127.0.0.1:{self.config.port}")
        return Check(True, f"{url} answers over HTTPS")


def report(checks: List[Check], color: bool = False) -> str:
    """The checks as lines: `✓ text`, or `✗ text — fix`."""
    lines = []
    for check in checks:
        mark = "✓" if check.ok else "✗"
        if color:
            mark = f"\033[32m{mark}\033[0m" if check.ok else f"\033[31m{mark}\033[0m"
        line = f"{mark} {check.text}"
        if not check.ok and check.fix:
            line += f" — {check.fix}"
        lines.append(line)
    return "\n".join(lines)
