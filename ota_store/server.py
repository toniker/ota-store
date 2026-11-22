"""The store's HTTP server: a JSON API, OTA manifests, the stored builds' files, and the page.

It reads the config on every request, so edits apply without a restart. It
listens on 127.0.0.1; something in front of it (`tailscale serve`, Caddy)
terminates HTTPS, which iOS requires for installs. See "HTTP API" in
docs/DESIGN.md for the routes and the JSON objects.
"""

from __future__ import annotations

import json
import mimetypes
import os
import plistlib
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import builds, config, fingerprint
from .builder import Builder, Job, clean_up_work, interrupt_on_termination
from .builds import Build
from .config import App, Config, ConfigError

WEB = Path(__file__).resolve().parent / "web"
# How long a fingerprint is reused: the page asks for every app's at once, and often.
FRESHNESS_TTL = 2.0
# How much of a build log the page gets: the end, where the errors are.
LOG_TAIL = 256 * 1024
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
STATIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
}
DISCONNECTS = (BrokenPipeError, ConnectionResetError)


class NotFound(Exception):
    """Answered with a 404."""


class Conflict(Exception):
    """Answered with a 409."""


def _tilde(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return str(Path("~") / path.relative_to(Path.home()))
    except ValueError:
        return str(path)


class Freshness:
    """Fingerprints apps, in parallel, remembering each result for `ttl` seconds."""

    def __init__(self, ttl: float = FRESHNESS_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        # app id -> (when, the app as configured then, the fingerprint or the error's message)
        self._cache: Dict[str, Tuple[float, Dict[str, Any], Union[fingerprint.Fingerprint, str]]] = {}

    def compute(self, app: App) -> Union[fingerprint.Fingerprint, str]:
        """The app's fingerprint, or why it couldn't be computed."""
        described = app.to_json()
        with self._lock:
            cached = self._cache.get(app.id)
        if cached is not None and cached[1] == described and time.monotonic() - cached[0] < self.ttl:
            return cached[2]
        try:
            result: Union[fingerprint.Fingerprint, str] = fingerprint.compute(app)
        except fingerprint.FingerprintError as error:
            result = str(error)
        except Exception as error:  # A bug there shouldn't take the list down with it.
            result = f"Can't fingerprint {app.id}: {error}"
        with self._lock:
            self._cache[app.id] = (time.monotonic(), described, result)
        return result

    def compute_all(self, apps: List[App]) -> Dict[str, Union[fingerprint.Fingerprint, str]]:
        buildable = [app for app in apps if app.buildable]
        if not buildable:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(buildable))) as pool:
            return dict(zip([app.id for app in buildable], pool.map(self.compute, buildable)))


class StoreServer(ThreadingHTTPServer):
    """The HTTP server, with the builder and freshness cache its requests share."""

    daemon_threads = True

    def __init__(self, address: Tuple[str, int], builder: Optional[Builder] = None,
                 freshness_ttl: float = FRESHNESS_TTL):
        self.builder = builder if builder is not None else Builder()
        self.freshness = Freshness(freshness_ttl)
        super().__init__(address, StoreHandler)

    def handle_error(self, request, client_address) -> None:
        # Phones hang up mid-download all the time; that's not worth a traceback.
        if isinstance(sys.exc_info()[1], DISCONNECTS):
            return
        super().handle_error(request, client_address)


class StoreHandler(BaseHTTPRequestHandler):
    server: StoreServer
    server_version = "ota-store"

    # MARK: - Plumbing

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        # The page polls the API every few seconds: those stay out of the log. Errors don't.
        if urlsplit(self.path).path.startswith("/api/"):
            return
        super().log_request(code, size)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "Content-Length isn't a number", True)
        if 0 < length <= 1 << 16:
            self.rfile.read(length)
        self._handle("POST")

    def _handle(self, method: str) -> None:
        url = urlsplit(self.path)
        parts = [unquote(part) for part in url.path.split("/")[1:]]
        query = parse_qs(url.query)
        is_api = url.path.startswith("/api/")
        self._config: Optional[Config] = None
        try:
            self._route(method, parts, query)
        except DISCONNECTS:
            pass
        except NotFound as error:
            self._error(HTTPStatus.NOT_FOUND, str(error) or "Not found", is_api)
        except Conflict as error:
            self._error(HTTPStatus.CONFLICT, str(error), True)
        except ConfigError as error:
            self.log_error("%s %s: %s", method, self.path, error)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error), True)
        except Exception as error:  # Keep serving; say what broke.
            self.log_error("%s %s: %r", method, self.path, error)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(error).__name__}: {error}", is_api)

    @property
    def config(self) -> Config:
        """The config as it is now, read once per request (and only by routes that need it)."""
        if self._config is None:
            self._config = config.load_config()
        return self._config

    def _error(self, status: HTTPStatus, message: str, as_json: bool) -> None:
        try:
            if as_json:
                self._send_json({"error": message}, status)
            else:
                self.send_error(status, message)
        except DISCONNECTS:
            pass

    def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK,
              cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(json.dumps(data).encode(), "application/json", status)

    def _send_file(self, path: Optional[Path], content_type: Optional[str] = None,
                   cache: str = "no-cache") -> None:
        """Streams a file. Opening it first means a build replaced meanwhile still arrives whole."""
        if path is None:
            raise NotFound()
        try:
            file = open(path, "rb")
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise NotFound()
        with file:
            size = os.fstat(file.fileno()).st_size
            if content_type is None:
                content_type = CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] \
                    or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            if self.command != "HEAD":
                shutil.copyfileobj(file, self.wfile, 1 << 16)

    # MARK: - Routes

    def _route(self, method: str, parts: List[str], query: Dict[str, List[str]]) -> None:
        # Only the last part may be empty ("/", "/api/apps/"); none may climb out or hide a slash.
        if any(part in (".", "..") or "/" in part or "\\" in part or "\0" in part for part in parts) \
                or "" in parts[:-1]:
            raise NotFound()
        if len(parts) > 1 and parts[-1] == "":
            parts = parts[:-1]
        if method == "POST":
            if len(parts) == 4 and parts[:2] == ["api", "apps"] and parts[3] == "install":
                force = (query.get("force") or [""])[-1].lower() in ("1", "true", "yes")
                return self._send_json(self._install(self._app(parts[2]), force))
            raise NotFound()

        if parts == [""] or parts == ["index.html"]:
            return self._send_file(WEB / "index.html")
        if parts == ["favicon.ico"]:
            return self._send_file(WEB / "favicon-32.png")
        if len(parts) == 2 and parts[0] == "static":
            if not STATIC_NAME.match(parts[1]):
                raise NotFound()
            return self._send_file(WEB / parts[1])

        if parts[:2] == ["api", "apps"]:
            if len(parts) == 2:
                return self._send_json(self._apps())
            app = self._app(parts[2])
            if len(parts) == 3:
                return self._send_json(self._app_detail(app))
            if len(parts) == 4 and parts[3] == "status":
                return self._send_json(self._status(app))
            if len(parts) == 4 and parts[3] == "log":
                return self._send_log(app)
            raise NotFound()

        if parts[:1] == ["apps"] and len(parts) in (3, 4):
            app = self._app(parts[1])
            if len(parts) == 3 and parts[2] == "icon.png":
                latest = builds.latest(app.id)
                return self._send_file(latest.icon_path if latest else None, "image/png")
            if len(parts) == 4:
                build = builds.get(app.id, parts[2])
                if build is None:
                    raise NotFound()
                if parts[3] == "manifest.plist":
                    return self._send(self._manifest(app, build), "application/xml", cache="no-cache")
                if parts[3] == "App.ipa":
                    return self._send_file(build.ipa_path, "application/octet-stream")
                if parts[3] == "icon.png":
                    return self._send_file(build.icon_path, "image/png")
        raise NotFound()

    def _app(self, app_id: str) -> App:
        app = self.config.app(app_id)
        if app is None:
            raise NotFound(f"No app with the id {app_id!r}")
        return app

    # MARK: - URLs

    def base_url(self) -> str:
        """Where phones reach the store: `config.url`, else the host this request was made to."""
        if self.config.url:
            return self.config.url
        return base_url_for(self.headers.get("Host"), self.config)

    def install_url(self, app: App, build: Build) -> str:
        manifest = f"{self.base_url()}/apps/{quote(app.id)}/{build.key}/manifest.plist"
        return "itms-services://?action=download-manifest&url=" + quote(manifest, safe="")

    # MARK: - Objects

    def _build_json(self, app: App, build: Build) -> Dict[str, Any]:
        profile = build.profile or {}
        return {
            "key": build.key,
            "name": build.name,
            "version": build.version,
            "build": build.build,
            "size": build.size,
            "builtAt": build.built_at,
            "duration": build.duration,
            "source": build.source,
            "configuration": build.configuration,
            "bundleId": build.bundle_id,
            "published": build.published,
            "expiresAt": profile.get("expiresAt"),
            "devices": profile.get("devices"),
            "installable": build.installable(),
            "installUrl": self.install_url(app, build),
            "iconUrl": f"/apps/{quote(app.id)}/{build.key}/icon.png" if build.has_icon else None,
        }

    def _job_json(self, app: App, job: Optional[Job]) -> Dict[str, Any]:
        if job is None:
            return {"state": "idle"}
        builder = self.server.builder
        status: Dict[str, Any] = {
            "state": job.state,
            "step": builder.waiting_step(job),
            "elapsed": int(job.elapsed),
        }
        if job.state == "failed":
            status["error"] = job.error
        if job.state == "done" and job.build is not None:
            status["build"] = self._build_json(app, job.build)
        return status

    def _done_json(self, app: App, build: Build) -> Dict[str, Any]:
        return {"state": "done", "step": "", "elapsed": 0, "cached": True, "build": self._build_json(app, build)}

    def _status(self, app: App) -> Dict[str, Any]:
        return self._job_json(app, self.server.builder.job(app.id))

    def _install(self, app: App, force: bool) -> Dict[str, Any]:
        if not app.buildable:
            latest = builds.latest(app.id)
            if latest is None:
                raise Conflict(f"{app.name} has no folder to build from and no installable build")
            builds.mark_used(latest)
            return self._done_json(app, latest)
        result = self.server.builder.request(app, force=force)
        if isinstance(result, Build):
            return self._done_json(app, result)
        return self._job_json(app, result)

    def _app_json(self, app: App, current: Union[fingerprint.Fingerprint, str, None]) -> Dict[str, Any]:
        stored = builds.list_builds(app.id)
        latest = next((build for build in stored if build.installable()), None)
        freshness: Optional[str] = None
        problem: Optional[str] = None
        if isinstance(current, str):
            problem = current
        elif current is not None:
            match = next((build for build in stored if build.fingerprint == current.key), None)
            if match is not None and match.installable():
                freshness = "current"
            elif stored:
                freshness = "changed"
            else:
                freshness = "unbuilt"
        return {
            "id": app.id,
            "name": (latest.name if latest else None) or app.name,
            "bundleId": latest.bundle_id if latest else None,
            "buildable": app.buildable,
            "freshness": freshness,
            "problem": problem,
            "latest": self._build_json(app, latest) if latest else None,
            "status": self._status(app),
            "iconUrl": f"/apps/{quote(app.id)}/icon.png" if latest and latest.has_icon else None,
        }

    def _apps(self) -> Dict[str, Any]:
        apps = list(self.config.apps.values())
        fingerprints = self.server.freshness.compute_all(apps)
        listed = [self._app_json(app, fingerprints.get(app.id)) for app in apps]
        # Newest build first, then the unbuilt ones, by name.
        listed.sort(key=lambda entry: entry["name"].lower())
        listed.sort(key=lambda entry: (entry["latest"] or {}).get("builtAt") or "", reverse=True)
        return {"title": self.config.title, "apps": listed}

    def _app_detail(self, app: App) -> Dict[str, Any]:
        current = self.server.freshness.compute(app) if app.buildable else None
        detail = self._app_json(app, current)
        try:
            commits = fingerprint.recent_commits(app) if app.buildable else []
        except Exception:  # The commits are a nicety; the rest of the page still works.
            commits = []
        detail["commits"] = [{"sha": commit.sha, "subject": commit.subject, "date": commit.date}
                             for commit in commits]
        detail["info"] = {
            "dir": _tilde(app.dir),
            "project": _tilde(app.project),
            "scheme": app.scheme,
            "configuration": app.configuration,
            "command": app.command,
        }
        detail["builds"] = [self._build_json(app, build) for build in builds.list_builds(app.id)]
        return detail

    def _manifest(self, app: App, build: Build) -> bytes:
        base = f"{self.base_url()}/apps/{quote(app.id)}/{build.key}"
        assets = [{"kind": "software-package", "url": f"{base}/App.ipa"}]
        if build.has_icon:
            for kind in ("display-image", "full-size-image"):
                assets.append({"kind": kind, "url": f"{base}/icon.png"})
        return plistlib.dumps({
            "items": [{
                "assets": assets,
                "metadata": {
                    "bundle-identifier": build.bundle_id,
                    "bundle-version": build.version,
                    "kind": "software",
                    "title": build.name or app.name,
                },
            }]
        })

    def _send_log(self, app: App) -> None:
        try:
            with open(config.log_path(app.id), "rb") as file:
                size = os.fstat(file.fileno()).st_size
                if size > LOG_TAIL:
                    file.seek(size - LOG_TAIL)
                    data = file.read()
                    # Start at a whole line.
                    newline = data.find(b"\n")
                    data = data[newline + 1:] if newline >= 0 else data
                else:
                    data = file.read()
        except FileNotFoundError:
            raise NotFound(f"{app.name} hasn't been built yet")
        self._send(data, "text/plain; charset=utf-8")


def base_url_for(host: Optional[str], settings: Config) -> str:
    """The base URL for a request made to `host`: https, except on this Mac, where there's no certificate."""
    if settings.url:
        return settings.url
    host = host or f"{settings.host}:{settings.port}"
    if host.startswith("["):
        name = host[:host.find("]") + 1]
    else:
        name = host.split(":", 1)[0]
    scheme = "http" if name.lower() in LOCAL_HOSTS else "https"
    return f"{scheme}://{host}"


def make_server(host: Optional[str] = None, port: Optional[int] = None,
                builder: Optional[Builder] = None, freshness_ttl: float = FRESHNESS_TTL) -> StoreServer:
    """The server, bound but not running. Port 0 picks a free one: see `server_address`."""
    settings = config.load_config()
    return StoreServer((host or settings.host, settings.port if port is None else port),
                       builder=builder, freshness_ttl=freshness_ttl)


def serve(host: Optional[str] = None, port: Optional[int] = None) -> None:
    """Runs the server until interrupted."""
    server = make_server(host, port)
    address, bound_port = server.server_address[:2]
    settings = config.load_config()
    print(f"ota-store serving on http://{address}:{bound_port} as {settings.url or 'the request host'}", flush=True)
    clean_up_work()
    interrupt_on_termination()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.builder.shutdown()
        server.server_close()
