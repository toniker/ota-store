"""Where the store keeps things, and the apps registered with it.

Everything the store writes lives under `home()`; the build caches it can
always recreate live under `cache()`. Both can be moved with environment
variables, which is also how the tests keep to temporary folders.

    home()/config.json                     settings and the registered apps
    home()/builds/<app id>/<key>/          one build: App.ipa, meta.json, icon.png
    home()/logs/<app id>.log               the output of the app's last build
    home()/logs/server.log                 the server's own log
    cache()/DerivedData/<app id>/          the app's warm DerivedData
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DEFAULT_TITLE = "Apps"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_CONFIGURATION = "Debug"
# Builds kept per app, so switching back to an earlier state installs at once.
DEFAULT_KEEP = 3


class ConfigError(Exception):
    """The config file can't be read, or says something the store can't use."""


# MARK: - Locations

def home() -> Path:
    return Path(os.environ.get("OTA_STORE_HOME") or "~/Library/Application Support/ota-store").expanduser()


def cache() -> Path:
    return Path(os.environ.get("OTA_STORE_CACHE") or "~/Library/Caches/ota-store").expanduser()


def config_path() -> Path:
    return home() / "config.json"


def builds_dir(app_id: str) -> Path:
    return home() / "builds" / app_id


def log_path(app_id: str) -> Path:
    return home() / "logs" / f"{app_id}.log"


def server_log_path() -> Path:
    return home() / "logs" / "server.log"


def derived_data(app_id: str) -> Path:
    return cache() / "DerivedData" / app_id


# MARK: - Apps

def _expand(path: str, base: Optional[Path]) -> Path:
    """`path` made absolute: `~` is the home folder, and relative paths are relative to `base`."""
    expanded = Path(os.path.expanduser(path))
    if not expanded.is_absolute():
        if base is None:
            raise ConfigError(f"{path!r} is relative, but the app has no dir")
        expanded = base / expanded
    return Path(os.path.normpath(expanded))


def _contract(path: Path, base: Optional[Path]) -> str:
    """The inverse of `_expand`: relative to `base` when inside it, else with `~` for the home folder."""
    if base is not None:
        try:
            return str(path.relative_to(base))
        except ValueError:
            pass
    user_home = Path.home()
    try:
        return str(Path("~") / path.relative_to(user_home))
    except ValueError:
        return str(path)


@dataclass
class App:
    """An app the store can list, and build if it has a `dir`.

    `dir` is where the build command runs and what the fingerprint covers,
    together with `inputs`. `project` and `scheme` are for the built-in Xcode
    builder; with a `command`, they're only passed on to it.
    """

    id: str
    name: str
    dir: Optional[Path] = None
    project: Optional[Path] = None
    scheme: Optional[str] = None
    configuration: str = DEFAULT_CONFIGURATION
    command: Optional[str] = None
    inputs: List[Path] = field(default_factory=list)
    icon: Optional[Path] = None
    keep: int = DEFAULT_KEEP
    # Keys this version doesn't know, written back as they were.
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def buildable(self) -> bool:
        return self.dir is not None

    @classmethod
    def from_json(cls, entry: Dict[str, Any]) -> "App":
        if not isinstance(entry, dict):
            raise ConfigError(f"An app should be an object, not {entry!r}")
        entry = dict(entry)
        app_id = entry.pop("id", None)
        if not isinstance(app_id, str) or not ID_PATTERN.match(app_id):
            raise ConfigError(
                f"App id {app_id!r} should be lowercase letters, digits and dashes, like 'my-app'")
        name = entry.pop("name", None) or app_id
        directory = entry.pop("dir", None)
        base = _expand(directory, None) if directory else None
        project = entry.pop("project", None)
        icon = entry.pop("icon", None)
        inputs = entry.pop("inputs", None) or []
        if not isinstance(inputs, list):
            raise ConfigError(f"{app_id}: inputs should be a list of paths")
        keep = entry.pop("keep", DEFAULT_KEEP)
        if not isinstance(keep, int) or keep < 1:
            raise ConfigError(f"{app_id}: keep should be a whole number of at least 1")
        return cls(
            id=app_id,
            name=str(name),
            dir=base,
            project=_expand(project, base) if project else None,
            scheme=entry.pop("scheme", None) or None,
            configuration=entry.pop("configuration", None) or DEFAULT_CONFIGURATION,
            command=entry.pop("command", None) or None,
            inputs=[_expand(path, base) for path in inputs],
            icon=_expand(icon, base) if icon else None,
            keep=keep,
            extra=entry,
        )

    def to_json(self) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"id": self.id, "name": self.name}
        if self.dir is not None:
            entry["dir"] = _contract(self.dir, None)
        if self.project is not None:
            entry["project"] = _contract(self.project, self.dir)
        if self.scheme:
            entry["scheme"] = self.scheme
        if self.configuration != DEFAULT_CONFIGURATION:
            entry["configuration"] = self.configuration
        if self.command:
            entry["command"] = self.command
        if self.inputs:
            entry["inputs"] = [_contract(path, self.dir) for path in self.inputs]
        if self.icon is not None:
            entry["icon"] = _contract(self.icon, self.dir)
        if self.keep != DEFAULT_KEEP:
            entry["keep"] = self.keep
        entry.update(self.extra)
        return entry


# MARK: - Config

@dataclass
class Config:
    title: str = DEFAULT_TITLE
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # The address phones reach the store at, like https://apps.example.ts.net.
    # Without it, the address each request was made to.
    url: Optional[str] = None
    apps: Dict[str, App] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    def app(self, app_id: str) -> Optional[App]:
        return self.apps.get(app_id)


def load_config(apply_environment: bool = True) -> Config:
    """The config file, or the defaults if there isn't one yet.

    With `apply_environment`, OTA_STORE_TITLE, OTA_STORE_HOST, OTA_STORE_PORT
    and OTA_STORE_URL override what the file says. Saving a config loaded that
    way would write the overrides into the file, so `save_config` callers
    load with `apply_environment=False`.
    """
    path = config_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        data: Dict[str, Any] = {}
    except OSError as error:
        raise ConfigError(f"Can't read {path}: {error.strerror}") from error
    else:
        try:
            data = json.loads(text)
        except ValueError as error:
            raise ConfigError(f"{path} isn't valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{path} should hold an object")

    data = dict(data)
    apps: Dict[str, App] = {}
    for entry in data.pop("apps", None) or []:
        app = App.from_json(entry)
        if app.id in apps:
            raise ConfigError(f"Two apps have the id {app.id!r}")
        apps[app.id] = app
    config = Config(
        title=str(data.pop("title", None) or DEFAULT_TITLE),
        host=str(data.pop("host", None) or DEFAULT_HOST),
        port=int(data.pop("port", None) or DEFAULT_PORT),
        url=(data.pop("url", None) or None),
        apps=apps,
        extra=data,
    )
    if apply_environment:
        config.title = os.environ.get("OTA_STORE_TITLE") or config.title
        config.host = os.environ.get("OTA_STORE_HOST") or config.host
        config.port = int(os.environ.get("OTA_STORE_PORT") or config.port)
        config.url = os.environ.get("OTA_STORE_URL") or config.url
    if config.url:
        config.url = config.url.rstrip("/")
    return config


def save_config(config: Config) -> None:
    """Writes the config file in one step, so a reader never sees half of it."""
    data: Dict[str, Any] = {}
    if config.title != DEFAULT_TITLE:
        data["title"] = config.title
    if config.host != DEFAULT_HOST:
        data["host"] = config.host
    if config.port != DEFAULT_PORT:
        data["port"] = config.port
    if config.url:
        data["url"] = config.url
    data.update(config.extra)
    data["apps"] = [app.to_json() for app in config.apps.values()]

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(data, file, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise
