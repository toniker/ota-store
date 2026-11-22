"""The `ota-store` command: for people in a terminal and for coding agents.

Output is short plain lines, coloured only on a terminal. Exit codes: 0 when
it worked, 1 when it didn't, 2 for a usage error. Every error says what to do
next. Modules that need Xcode, the builder or the server are imported inside
the commands that use them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO
from urllib.parse import quote, urlsplit

from . import __version__
from . import config as config_module
from .config import App, Config, ConfigError, ID_PATTERN, load_config, save_config

# How often `build` asks the server how its build is going.
POLL_INTERVAL = 1.0


class CommandError(Exception):
    """A command failed. `hint` says what to do about it."""

    def __init__(self, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.hint = hint


def out(text: str = "") -> None:
    print(text, flush=True)


def color(text: str, code: str, stream: TextIO = sys.stdout) -> str:
    if os.environ.get("NO_COLOR") or not stream.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def duration(seconds: float) -> str:
    """Seconds as m:ss, or h:mm:ss."""
    seconds = int(round(max(seconds, 0)))
    hours, rest = divmod(seconds, 3600)
    if hours:
        return f"{hours}:{rest // 60:02d}:{rest % 60:02d}"
    return f"{rest // 60}:{rest % 60:02d}"


def ago(timestamp: str, now: Optional[datetime] = None) -> str:
    """An ISO 8601 time as "3 min ago", "2 h ago", "4 d ago"."""
    try:
        then = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return "?"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = ((now or datetime.now(timezone.utc)) - then).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def slugify(text: str) -> str:
    """An app id made from a name: "WeatherDeck" → "weatherdeck", "My App 2" → "my-app-2"."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:64].rstrip("-")


def tilde(path: Path) -> str:
    try:
        return str(Path("~") / Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def read_config(apply_environment: bool = True) -> Config:
    try:
        return load_config(apply_environment=apply_environment)
    except ConfigError as error:
        raise CommandError(str(error), f"Fix {config_module.config_path()}, then try again.") from error


def require_app(config: Config, app_id: str) -> App:
    app = config.app(app_id)
    if app is None:
        known = ", ".join(sorted(config.apps)) or "none yet"
        raise CommandError(f"No app with id {app_id!r} (registered: {known})",
                           "See them with `ota-store list`, or register one with `ota-store add DIR`.")
    return app


def public_url(config: Config) -> Optional[str]:
    """The URL phones open: the config's `url`, else what `tailscale serve` proxies to the port."""
    from .doctor import public_url as find

    return find(config)


def install_url(base: str, app_id: str, key: str) -> str:
    manifest = f"{base}/apps/{quote(app_id)}/{quote(key)}/manifest.plist"
    return "itms-services://?action=download-manifest&url=" + quote(manifest, safe="")


# MARK: - Project discovery

def find_project(folder: Path) -> Path:
    """The .xcworkspace or .xcodeproj the built-in builder would use in `folder`. Raises CommandError."""
    try:
        from . import xcode_build
    except ImportError:
        xcode_build = None  # type: ignore[assignment]
    finder = getattr(xcode_build, "find_project", None)
    if finder is not None:
        try:
            return Path(finder(folder))
        except Exception as error:
            raise CommandError(str(error), "Name it with --project, or use --command for another build tool.") from error
    return _find_project(folder)


def find_scheme(project: Path, preferred: Optional[str] = None) -> str:
    """The scheme the built-in builder would build. Raises CommandError."""
    try:
        from . import xcode_build
    except ImportError:
        xcode_build = None  # type: ignore[assignment]
    finder = getattr(xcode_build, "find_scheme", None)
    if finder is not None:
        try:
            return str(finder(project, preferred))
        except Exception as error:
            raise CommandError(str(error), "Name it with --scheme.") from error
    return _find_scheme(project, preferred)


def _find_project(folder: Path) -> Path:
    """Stand-in for xcode_build.find_project: the one workspace, else the one project, in `folder`."""
    workspaces = sorted(folder.glob("*.xcworkspace"))
    projects = sorted(folder.glob("*.xcodeproj"))
    for candidates in (workspaces, projects):
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            names = ", ".join(path.name for path in candidates)
            raise CommandError(f"{tilde(folder)} has more than one: {names}", "Pick one with --project.")
    raise CommandError(f"No .xcworkspace or .xcodeproj in {tilde(folder)}",
                       "Point at the folder that has one, name it with --project, "
                       "or give a build command with --command.")


def _find_scheme(project: Path, preferred: Optional[str] = None) -> str:
    """Stand-in for xcode_build.find_scheme: `preferred`, the only scheme, or the one named like the project."""
    if preferred:
        return preferred
    kind = "-workspace" if project.suffix == ".xcworkspace" else "-project"
    schemes: List[str] = []
    try:
        result = subprocess.run(["xcodebuild", "-list", "-json", kind, str(project)],
                                capture_output=True, text=True, timeout=120)
        listing = json.loads(result.stdout or "{}")
        schemes = (listing.get("workspace") or listing.get("project") or {}).get("schemes") or []
    except (OSError, ValueError, subprocess.SubprocessError):
        schemes = [path.stem for path in sorted(project.glob("xcshareddata/xcschemes/*.xcscheme"))]
    if len(schemes) == 1:
        return schemes[0]
    if project.stem in schemes:
        return project.stem
    if not schemes:
        raise CommandError(f"Found no schemes in {project.name}", "Name one with --scheme.")
    raise CommandError(f"{project.name} has several schemes: {', '.join(schemes)}", "Pick one with --scheme.")


# MARK: - Commands

def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(host=args.host, port=args.port)
    return 0


def _path_in(folder: Path, value: str, what: str, flag: str) -> Path:
    path = Path(os.path.normpath(folder / Path(value).expanduser()))
    if not path.exists():
        raise CommandError(f"{what} not found: {path}", f"Check {flag}; relative paths are relative to the app's folder.")
    return path


def cmd_add(args: argparse.Namespace) -> int:
    folder = Path(args.dir).expanduser().resolve()
    if not folder.is_dir():
        raise CommandError(f"Not a folder: {folder}", "Give the folder with the Xcode project: ota-store add DIR")

    project = _path_in(folder, args.project, "Project", "--project") if args.project else None
    if project is not None and project.suffix not in (".xcodeproj", ".xcworkspace") and not args.command:
        raise CommandError(f"{project.name} isn't an .xcodeproj or .xcworkspace", "Check --project.")
    scheme = args.scheme
    if not args.command:
        project = project or find_project(folder)
        scheme = find_scheme(project, args.scheme)
    inputs = [_path_in(folder, value, "Input", "--input") for value in args.input]
    icon = _path_in(folder, args.icon, "Icon", "--icon") if args.icon else None

    name = args.name or scheme or folder.name
    app_id = args.id or slugify(name)
    if not ID_PATTERN.match(app_id):
        raise CommandError(f"{app_id!r} can't be an id", "Give one with --id: lowercase letters, digits and dashes, like my-app.")

    config = read_config(apply_environment=False)
    if app_id in config.apps:
        other = config.apps[app_id]
        where = f" ({tilde(other.dir)})" if other.dir else ""
        raise CommandError(f"There's already an app with id {app_id!r}{where}",
                           f"Pick another with --id, or remove it first: ota-store remove {app_id}")
    for other in config.apps.values():
        # One folder can hold several apps, as long as they build differently.
        if other.dir == folder and other.scheme == scheme and other.command == args.command:
            raise CommandError(f"{tilde(folder)} is already registered as {other.id!r}",
                               f"Build it with `ota-store build {other.id}`, or give another --scheme or --command.")

    app = App(id=app_id, name=name, dir=folder, project=project, scheme=scheme,
              configuration=args.configuration or config_module.DEFAULT_CONFIGURATION,
              command=args.command, inputs=inputs, icon=icon)
    config.apps[app_id] = app
    save_config(config)

    out(f"Registered {app_id} ({name})")
    out(f"  dir            {tilde(folder)}")
    if project is not None:
        out(f"  project        {project.relative_to(folder) if folder in project.parents else tilde(project)}")
    if scheme:
        out(f"  scheme         {scheme}")
    out(f"  configuration  {app.configuration}")
    if args.command:
        out(f"  command        {args.command}")
    for path in inputs:
        out(f"  input          {tilde(path)}")
    out(f"Build it: ota-store build {app_id}")
    return 0


def _freshness(app: App) -> Dict[str, Any]:
    """What `list` shows for an app: freshness, problem and latest build, worked out without the server."""
    from . import builds
    from .fingerprint import FingerprintError, compute

    info: Dict[str, Any] = {"freshness": None, "problem": None, "latest": builds.latest(app.id)}
    if not app.buildable:
        return info
    try:
        fingerprint = compute(app)
    except FingerprintError as error:
        info["problem"] = str(error)
        return info
    match = builds.find(app.id, fingerprint.key)
    if match is not None and match.installable():
        info["freshness"] = "current"
    elif builds.list_builds(app.id):
        info["freshness"] = "changed"
    else:
        info["freshness"] = "unbuilt"
    return info


def cmd_list(args: argparse.Namespace) -> int:
    config = read_config()
    if not config.apps:
        if args.json:
            out("[]")
        else:
            out("No apps yet. Register one: ota-store add ~/path/to/app")
        return 0
    rows = []
    for app in config.apps.values():
        try:
            info = _freshness(app)
        except ImportError:
            info = {"freshness": None, "problem": "builds module unavailable", "latest": None}
        latest = info["latest"]
        rows.append({
            "id": app.id,
            "name": app.name,
            "buildable": app.buildable,
            "freshness": info["freshness"],
            "problem": info["problem"],
            "version": f"{latest.version} ({latest.build})" if latest else None,
            "builtAt": latest.built_at if latest else None,
        })
    if args.json:
        out(json.dumps(rows, indent=2))
        return 0
    table = [["ID", "NAME", "STATE", "LATEST", "BUILT"]]
    for row in rows:
        state = row["freshness"] or ("published" if not row["buildable"] else "problem")
        table.append([row["id"], row["name"], state, row["version"] or "-",
                      ago(row["builtAt"]) if row["builtAt"] else "-"])
    widths = [max(len(line[column]) for line in table) for column in range(len(table[0]))]
    for line in table:
        out("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip())
    for row in rows:
        if row["problem"]:
            out(f"{row['id']}: {row['problem']}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    config = read_config(apply_environment=False)
    app = config.apps.pop(args.id, None)
    if app is None and not args.purge:
        require_app(config, args.id)
    if app is not None:
        save_config(config)
        out(f"Removed {args.id}")
    if args.purge:
        try:
            from . import builds

            builds.delete_all(args.id)
        except ImportError:
            shutil.rmtree(config_module.builds_dir(args.id), ignore_errors=True)
        shutil.rmtree(config_module.derived_data(args.id), ignore_errors=True)
        try:
            config_module.log_path(args.id).unlink()
        except FileNotFoundError:
            pass
        out(f"Deleted the builds, log and DerivedData of {args.id}")
    elif app is not None:
        out(f"Its builds are kept; delete them with: ota-store remove {args.id} --purge")
    return 0


# MARK: - build

class ServerUnavailable(Exception):
    """Nothing answers on the store's port."""


class ServerSlow(Exception):
    """The server took the connection but didn't answer in time."""


class Client:
    """The running server's JSON API, at `base` (http://127.0.0.1:8765)."""

    def __init__(self, base: str, host: Optional[str] = None, timeout: float = 30):
        self.base = base
        # Sent as the Host header, so install URLs come out with the public name.
        self.host = host
        self.timeout = timeout

    def request(self, method: str, path: str) -> Dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.host:
            headers["Host"] = self.host
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                                         data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            body = error.read()
            try:
                message = json.loads(body).get("error")
            except (ValueError, AttributeError):
                message = None
            if message is None:
                raise CommandError(f"Something on {self.base} answered {error.code} but isn't ota-store (an older store?)",
                                   "Stop it, or move the store to another port; `ota-store doctor` shows what's running.") from error
            raise CommandError(message, "See `ota-store list`, or `ota-store doctor`.") from error
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, (ConnectionRefusedError, ConnectionResetError, FileNotFoundError)):
                raise ServerUnavailable(str(reason)) from error
            raise ServerSlow(f"{self.base} didn't answer ({reason})") from error
        try:
            answer = json.loads(body)
        except ValueError as error:
            raise CommandError(f"{self.base}{path} didn't answer with JSON (an older store?)",
                               "`ota-store doctor` shows what's running on the port.") from error
        if not isinstance(answer, dict):
            raise CommandError(f"{self.base}{path} answered something unexpected", "Try `ota-store doctor`.")
        return answer


def _print_step(started: float, step: str) -> None:
    out(f"{duration(time.monotonic() - started):>5}  {step}")


def _print_done(app: App, version: str, build_number: str, cached: bool, took: Optional[float],
                url: Optional[str], base: Optional[str], ipa: Optional[Path] = None) -> None:
    how = "from a stored build, nothing changed" if cached else f"built in {duration(took or 0)}"
    out(f"Done: {app.name} {version} ({build_number}), {how}")
    if url:
        out(f"Install: {url}")
    if base:
        out(f"Open on the phone: {base}/#/apps/{app.id}")
        return
    if ipa is not None:
        out(f"IPA: {ipa}")
    out("No public HTTPS URL, so phones can't install it yet; see `ota-store url`.")


def build_via_server(app: App, client: Client, force: bool, base: Optional[str]) -> int:
    """Asks the server to build, as a tap would, and follows the build. Raises ServerUnavailable."""
    path = f"/api/apps/{quote(app.id)}"
    started = time.monotonic()
    try:
        status = client.request("POST", path + "/install" + ("?force=1" if force else ""))
    except ServerSlow as error:
        raise CommandError(str(error), "Check the server with `ota-store doctor`, then try again.") from error
    shown = None
    lost_at: Optional[float] = None
    while status.get("state") in ("queued", "building"):
        step = status.get("step") or status.get("state")
        if step != shown:
            _print_step(started, step)
            shown = step
        time.sleep(POLL_INTERVAL)
        try:
            status = client.request("GET", path + "/status")
            lost_at = None
        except (ServerUnavailable, ServerSlow):
            # A restarting server comes back within seconds under launchd.
            lost_at = lost_at or time.monotonic()
            if time.monotonic() - lost_at > 30:
                raise CommandError("Lost the server while building",
                                   "Check `ota-store service status`, then run the build again.")

    state = status.get("state")
    if state == "done" and status.get("build"):
        build = status["build"]
        _print_done(app, build.get("version", "?"), build.get("build", "?"), bool(status.get("cached")),
                    status.get("elapsed"), build.get("installUrl"), base)
        return 0
    if state == "failed":
        out(f"Failed: {status.get('error') or 'the build failed'}")
        out(f"Log: {config_module.log_path(app.id)} (or: ota-store log {app.id})")
        return 1
    raise CommandError(f"The server stopped tracking the build (state {state!r})",
                       f"It may have restarted; run `ota-store build {app.id}` again.")


def build_in_process(app: App, force: bool, base: Optional[str]) -> int:
    """Builds with a Builder in this process, for when no server is running."""
    try:
        from .builder import Builder, interrupt_on_termination
    except ImportError as error:
        raise CommandError(f"Can't build here: {error}", "Start the server (ota-store serve) and try again.") from error
    builder = Builder()
    started = time.monotonic()
    result = builder.request(app, force=force)
    if not hasattr(result, "state"):
        _print_done(app, result.version, result.build, True, None,
                    install_url(base, app.id, result.key) if base else None, base, result.ipa_path)
        return 0
    interrupt_on_termination()
    try:
        job = builder.wait(result, on_step=lambda step: _print_step(started, step))
    except KeyboardInterrupt:
        # The build runs in its own process group, so it would outlive this process.
        builder.shutdown()
        raise
    if job.state == "done" and job.build is not None:
        _print_done(app, job.build.version, job.build.build, False, time.monotonic() - started,
                    install_url(base, app.id, job.build.key) if base else None, base, job.build.ipa_path)
        return 0
    out(f"Failed: {job.error or 'the build failed'}")
    out(f"Log: {config_module.log_path(app.id)} (or: ota-store log {app.id})")
    return 1


def cmd_build(args: argparse.Namespace) -> int:
    config = read_config()
    app = require_app(config, args.id)
    if not app.buildable:
        raise CommandError(f"{app.id} has no folder to build from; it only installs published builds",
                           f"Publish a new one with `ota-store publish App.ipa --id {app.id}`, "
                           "or register its folder with `ota-store add DIR`.")
    base = public_url(config)
    host = urlsplit(base).netloc if base and not config.url else None
    address = f"http://{local_address(config)}"
    try:
        return build_via_server(app, Client(address, host=host), args.force, base)
    except ServerUnavailable:
        out(f"No server on {address}; building here. (Start one: ota-store service install)")
    return build_in_process(app, args.force, base)


def local_address(config: Config) -> str:
    from .doctor import local_address as address

    return address(config)


# MARK: - The other commands

def cmd_publish(args: argparse.Namespace) -> int:
    ipa = Path(args.ipa).expanduser().resolve()
    if not ipa.is_file():
        raise CommandError(f"No file at {ipa}", "Give the path of an .ipa: ota-store publish App.ipa --id my-app")
    if not ID_PATTERN.match(args.id):
        raise CommandError(f"{args.id!r} can't be an id", "Use lowercase letters, digits and dashes, like my-app.")
    from . import builds

    config = read_config(apply_environment=False)
    try:
        build = builds.publish(args.id, ipa, name=args.name)
    except builds.InvalidIPA as error:
        raise CommandError(f"{ipa.name} isn't a usable .ipa: {error}", "Check it was exported for iOS devices.") from error
    if args.id not in config.apps:
        config.apps[args.id] = App(id=args.id, name=args.name or build.name or args.id)
        save_config(config)
        out(f"Registered {args.id} ({config.apps[args.id].name}), without a folder")
    out(f"Published {args.id}: {build.name} {build.version} ({build.build})")
    base = public_url(read_config())
    if base:
        out(f"Install: {install_url(base, args.id, build.key)}")
        out(f"Open on the phone: {base}/#/apps/{args.id}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    path = config_module.log_path(args.id)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise CommandError(f"No build log for {args.id} yet", f"Build it: ota-store build {args.id}") from None
    sys.stdout.flush()
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(data)
    else:
        sys.stdout.write(data.decode("utf-8", "replace"))
    sys.stdout.flush()
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    config = read_config()
    url = public_url(config)
    if url is None:
        raise CommandError("No public URL yet",
                           f"Run `tailscale serve --bg --https=443 http://127.0.0.1:{config.port}`, "
                           f"or set \"url\" in {config_module.config_path()} for another HTTPS proxy.")
    out(url)
    return 0


def cmd_xcode_build(args: argparse.Namespace) -> int:
    from . import xcode_build

    return int(xcode_build.main(args.arguments) or 0)


def make_service():
    """The real LaunchAgent. Tests replace this."""
    from .service import Service

    return Service()


def cmd_service(args: argparse.Namespace) -> int:
    from .service import LABEL, ServiceError

    service = make_service()
    try:
        if args.action == "install":
            status = service.install()
            out(f"Installed {LABEL}: {status.describe()}")
            out(f"  plist  {tilde(service.plist_path)}")
            out(f"  log    {tilde(config_module.server_log_path())}")
            config = read_config()
            out(f"Serving on http://{local_address(config)}; put HTTPS in front of it: "
                f"tailscale serve --bg --https=443 http://127.0.0.1:{config.port}")
        elif args.action == "uninstall":
            service.uninstall()
            out(f"Uninstalled {LABEL}")
        elif args.action == "start":
            out(f"{LABEL}: {service.start().describe()}")
        elif args.action == "stop":
            service.stop()
            out(f"Stopped {LABEL} (it starts again at login, or with: ota-store service start)")
        elif args.action == "restart":
            out(f"{LABEL}: {service.restart().describe()}")
        else:
            status = service.status()
            out(f"{LABEL}: {status.describe()}")
            out(f"  plist  {tilde(service.plist_path)}{'' if status.installed else ' (missing)'}")
            out(f"  log    {tilde(config_module.server_log_path())}")
            if not status.installed:
                out("Install it: ota-store service install")
    except ServiceError as error:
        raise CommandError(str(error)) from error
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import Doctor, report

    checks = []
    # Printed as they finish: the network checks can take a few seconds.
    for check in Doctor().checks():
        checks.append(check)
        out(report([check], color=not os.environ.get("NO_COLOR") and sys.stdout.isatty()))
    failed = sum(1 for check in checks if not check.ok)
    out("All good." if not failed else f"{failed} problem{'' if failed == 1 else 's'}.")
    return 1 if failed else 0


# MARK: - Arguments

class Parser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors end with how to get help."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\nSee: {self.prog} --help\n")


def make_parser() -> argparse.ArgumentParser:
    parser = Parser(prog="ota-store",
                    description="A private iOS app store on your Mac: tap Install, it builds and installs.")
    parser.add_argument("--version", action="version", version=f"ota-store {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=Parser)

    serve = commands.add_parser("serve", help="run the server in the foreground")
    serve.add_argument("--host", help="address to listen on (default from the config: 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port (default from the config: 8765)")
    serve.set_defaults(handler=cmd_serve)

    add = commands.add_parser("add", help="register an app",
                              description="Registers an app. Without --command, finds the Xcode project and "
                                          "scheme as the built-in builder would. Paths are relative to DIR.")
    add.add_argument("dir", nargs="?", default=".", metavar="DIR", help="the app's folder (default: .)")
    add.add_argument("--id", help="id in URLs and commands (default: the scheme, lowercased)")
    add.add_argument("--name", help="name shown before the first build (default: the scheme)")
    add.add_argument("--project", help="the .xcworkspace or .xcodeproj")
    add.add_argument("--scheme", help="the scheme to build")
    add.add_argument("--configuration", help="build configuration (default: Debug)")
    add.add_argument("--command", help="a shell command that writes the .ipa to $OTA_OUTPUT, instead of the built-in builder")
    add.add_argument("--input", action="append", default=[], metavar="PATH",
                     help="another folder or file the build depends on (repeatable)")
    add.add_argument("--icon", help="the icon: a PNG, .icon or .appiconset")
    add.set_defaults(handler=cmd_add)

    listing = commands.add_parser("list", help="apps, whether they changed, and their latest build")
    listing.add_argument("--json", action="store_true", help="print JSON")
    listing.set_defaults(handler=cmd_list)

    remove = commands.add_parser("remove", help="unregister an app")
    remove.add_argument("id", metavar="ID")
    remove.add_argument("--purge", action="store_true", help="also delete its builds, log and DerivedData")
    remove.set_defaults(handler=cmd_remove)

    build = commands.add_parser("build", help="build an app as a tap would, and follow it",
                                description="Asks the running server to build the app, or reuses an unchanged "
                                            "build, and prints its steps. Builds here if no server answers.")
    build.add_argument("id", metavar="ID")
    build.add_argument("--force", action="store_true", help="build even if nothing changed (e.g. after adding a device)")
    build.set_defaults(handler=cmd_build)

    publish = commands.add_parser("publish", help="store an .ipa built elsewhere")
    publish.add_argument("ipa", metavar="IPA")
    publish.add_argument("--id", required=True, help="the app's id (registered without a folder if new)")
    publish.add_argument("--name", help="name to show (default: the app's display name)")
    publish.set_defaults(handler=cmd_publish)

    log = commands.add_parser("log", help="print the last build's log")
    log.add_argument("id", metavar="ID")
    log.set_defaults(handler=cmd_log)

    url = commands.add_parser("url", help="print the URL phones open")
    url.set_defaults(handler=cmd_url)

    xcode = commands.add_parser("xcode-build", help="the built-in Xcode builder, for build commands",
                                add_help=False)
    xcode.add_argument("arguments", nargs=argparse.REMAINDER)
    xcode.set_defaults(handler=cmd_xcode_build)

    service = commands.add_parser("service", help="the LaunchAgent that runs the server")
    service.add_argument("action", choices=["install", "uninstall", "start", "stop", "restart", "status"])
    service.set_defaults(handler=cmd_service)

    doctor = commands.add_parser("doctor", help="check Xcode, signing, the config, the server and HTTPS")
    doctor.set_defaults(handler=cmd_doctor)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["xcode-build"]:
        # Everything after it is the builder's, options included.
        return cmd_xcode_build(argparse.Namespace(arguments=argv[1:]))
    parser = make_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help(sys.stderr)
        return 2
    try:
        return args.handler(args)
    except CommandError as error:
        print(color(f"error: {error}", "31", sys.stderr), file=sys.stderr)
        if error.hint:
            print(error.hint, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if args.command == "build":
            print("\nStopped following. A build on the server goes on; see `ota-store list`.", file=sys.stderr)
        return 130
