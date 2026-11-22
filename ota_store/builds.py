"""Stored builds: the .ipa files the store can install, and what's known about each.

    builds/<app id>/<key>/App.ipa      the build
    builds/<app id>/<key>/meta.json    what it is: `Build`, in camelCase
    builds/<app id>/<key>/icon.png     its icon, if one was found

`key` is the first 16 hex digits of the fingerprint the build was made from,
or of the .ipa's sha256 for published ones. A build is written to a
temporary folder next to the others and renamed into place, so a reader sees
all of it or none. Folders whose names start with a dot are work in progress
or on their way out, never builds.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config, icons
from .config import App
from .fingerprint import Fingerprint

KEY_PATTERN = re.compile(r"^[0-9a-f]{16}$")
# A build whose profile expires sooner than this won't install by the time it's downloaded.
EXPIRY_MARGIN = timedelta(hours=1)
# Temporary folders older than this were left by a store that stopped mid-write.
STALE_AFTER = 24 * 60 * 60

_INFO_PLIST = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")
_PROFILE = re.compile(r"^Payload/[^/]+\.app/embedded\.mobileprovision$")

# Serialises the writers in this process: storing, marking used, pruning and deleting.
_lock = threading.RLock()


class InvalidIPA(Exception):
    """The file isn't an .ipa, or has no bundle id in it."""


@dataclass
class Build:
    """A stored build, as its meta.json describes it, and the folder it's in."""

    key: str
    fingerprint: str
    app_id: str
    name: str
    bundle_id: str
    version: str
    build: str
    size: int
    built_at: str
    last_used_at: str
    duration: Optional[float]
    source: str
    configuration: Optional[str]
    profile: Optional[Dict[str, Any]]
    has_icon: bool
    published: bool
    path: Path

    @property
    def ipa_path(self) -> Path:
        return self.path / "App.ipa"

    @property
    def icon_path(self) -> Optional[Path]:
        return self.path / "icon.png" if self.has_icon else None

    @property
    def expires_at(self) -> Optional[datetime]:
        expires = (self.profile or {}).get("expiresAt")
        if not expires:
            return None
        try:
            return datetime.fromisoformat(expires)
        except ValueError:
            return None

    def installable(self, now: Optional[datetime] = None) -> bool:
        """Whether a phone can install it: the .ipa is there and its profile isn't about to expire."""
        if not self.ipa_path.is_file():
            return False
        expires = self.expires_at
        if expires is None:
            return True
        return expires > (now or datetime.now(timezone.utc)) + EXPIRY_MARGIN

    def to_meta(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "fingerprint": self.fingerprint,
            "appId": self.app_id,
            "name": self.name,
            "bundleId": self.bundle_id,
            "version": self.version,
            "build": self.build,
            "size": self.size,
            "builtAt": self.built_at,
            "lastUsedAt": self.last_used_at,
            "duration": self.duration,
            "source": self.source,
            "configuration": self.configuration,
            "profile": self.profile,
            "hasIcon": self.has_icon,
            "published": self.published,
        }

    @classmethod
    def from_meta(cls, meta: Dict[str, Any], path: Path) -> "Build":
        """Raises KeyError, TypeError or ValueError for a meta.json that isn't one."""
        return cls(
            key=str(meta["key"]),
            fingerprint=str(meta.get("fingerprint") or ""),
            app_id=str(meta["appId"]),
            name=str(meta.get("name") or ""),
            bundle_id=str(meta["bundleId"]),
            version=str(meta.get("version") or ""),
            build=str(meta.get("build") or ""),
            size=int(meta.get("size") or 0),
            built_at=str(meta["builtAt"]),
            last_used_at=str(meta.get("lastUsedAt") or meta["builtAt"]),
            duration=float(meta["duration"]) if meta.get("duration") is not None else None,
            source=str(meta.get("source") or ""),
            configuration=meta.get("configuration"),
            profile=meta.get("profile") if isinstance(meta.get("profile"), dict) else None,
            has_icon=bool(meta.get("hasIcon")),
            published=bool(meta.get("published")),
            path=path,
        )


# MARK: - Reading .ipa files

def read_profile(data: bytes) -> Optional[Dict[str, Any]]:
    """The name, team, expiry and device count of an embedded.mobileprovision, or None if unreadable.

    The profile is a plist inside a CMS envelope; the plist is cut out as is,
    without verifying the signature, which installd does anyway.
    """
    start = data.find(b"<?xml")
    end = data.find(b"</plist>", start)
    if start < 0 or end < 0:
        return None
    try:
        plist = plistlib.loads(data[start:end + len(b"</plist>")])
    except Exception:  # plistlib raises several kinds for bad input
        return None
    if not isinstance(plist, dict):
        return None
    expires = plist.get("ExpirationDate")
    if isinstance(expires, datetime):
        # plistlib gives naive datetimes in UTC.
        expires_at: Optional[str] = (expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)).isoformat()
    else:
        expires_at = None
    if plist.get("ProvisionsAllDevices"):
        devices: Optional[int] = None
    else:
        devices = len(plist.get("ProvisionedDevices") or [])
    return {
        "name": plist.get("Name"),
        "team": plist.get("TeamName"),
        "expiresAt": expires_at,
        "devices": devices,
    }


def inspect_ipa(ipa: Path) -> Dict[str, Any]:
    """The name, bundle id, version, build and profile of the app in `ipa`. Raises InvalidIPA."""
    try:
        with zipfile.ZipFile(ipa) as archive:
            names = archive.namelist()
            info_name = next((name for name in names if _INFO_PLIST.match(name)), None)
            if info_name is None:
                raise InvalidIPA(f"{ipa} has no Payload/<name>.app/Info.plist, so it isn't an .ipa")
            try:
                info = plistlib.loads(archive.read(info_name))
            except Exception as error:
                raise InvalidIPA(f"{ipa}: can't read {info_name}: {error}") from error
            app_folder = info_name[:-len("Info.plist")]
            profile_name = app_folder + "embedded.mobileprovision"
            profile = read_profile(archive.read(profile_name)) if profile_name in names else None
    except (zipfile.BadZipFile, OSError) as error:
        raise InvalidIPA(f"{ipa} isn't an .ipa: {error}") from error
    if not isinstance(info, dict) or not info.get("CFBundleIdentifier"):
        raise InvalidIPA(f"{ipa} has no bundle id in its Info.plist")
    return {
        "name": str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or ""),
        "bundle_id": str(info["CFBundleIdentifier"]),
        "version": str(info.get("CFBundleShortVersionString") or ""),
        "build": str(info.get("CFBundleVersion") or ""),
        "profile": profile,
    }


# MARK: - Finding builds

def _app_folder(app_id: str) -> Optional[Path]:
    """builds/<app id>, or None for an id that could escape it."""
    if not config.ID_PATTERN.match(app_id or ""):
        return None
    return config.builds_dir(app_id)


def _read(path: Path) -> Optional[Build]:
    try:
        meta = json.loads((path / "meta.json").read_text())
        return Build.from_meta(meta, path)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def list_builds(app_id: str) -> List[Build]:
    """The app's stored builds, newest `built_at` first. Skips folders that aren't readable builds."""
    folder = _app_folder(app_id)
    if folder is None:
        return []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    found = []
    for entry in entries:
        if entry.name.startswith(".") or not KEY_PATTERN.match(entry.name):
            continue
        build = _read(Path(entry.path))
        if build is not None:
            found.append(build)
    found.sort(key=lambda build: (build.built_at, build.key), reverse=True)
    return found


def get(app_id: str, key: str) -> Optional[Build]:
    folder = _app_folder(app_id)
    if folder is None or not KEY_PATTERN.match(key or ""):
        return None
    return _read(folder / key)


def find(app_id: str, fingerprint: str) -> Optional[Build]:
    """The stored build made from `fingerprint` (a full key), installable or not."""
    build = get(app_id, fingerprint[:16])
    if build is None or build.fingerprint != fingerprint:
        return None
    return build


def latest(app_id: str) -> Optional[Build]:
    """The newest installable build."""
    now = datetime.now(timezone.utc)
    return next((build for build in list_builds(app_id) if build.installable(now)), None)


# MARK: - Writing builds

def _now() -> str:
    # Milliseconds keep builds stored within a second in order, and JavaScript's Date reads them.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    """Writes `path` in one step, so a reader never sees half of it."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".meta-", suffix=".json")
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(data, file, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _remove(path: Path) -> None:
    """Deletes a folder, renaming it out of the way first so readers never see it half deleted.

    A phone downloading from it keeps going: an open file outlives its name.
    """
    trash = path.with_name(f".old-{uuid.uuid4().hex}")
    try:
        os.rename(path, trash)
    except FileNotFoundError:
        return
    shutil.rmtree(trash, ignore_errors=True)


def _move_into_place(temporary: Path, target: Path) -> None:
    try:
        os.rename(temporary, target)
        return
    except OSError:
        if not target.is_dir():
            raise
    # A build with this key is there already (a forced rebuild of the same
    # sources). Replace it file by file: each replace is atomic, there's never
    # a moment without a build, and a download of the old .ipa carries on.
    # meta.json goes last, so it never describes files that aren't there yet.
    os.replace(temporary / "App.ipa", target / "App.ipa")
    new_icon = temporary / "icon.png"
    if new_icon.exists():
        os.replace(new_icon, target / "icon.png")
    os.replace(temporary / "meta.json", target / "meta.json")
    if not new_icon.exists():
        try:
            (target / "icon.png").unlink()
        except FileNotFoundError:
            pass
    shutil.rmtree(temporary, ignore_errors=True)


def _store(app_id: str, key: str, ipa: Path, move: bool, icon_app: Optional[App], keep: int,
           fingerprint: str, name: Optional[str], duration: Optional[float], source: str,
           configuration: Optional[str], published: bool) -> Build:
    folder = _app_folder(app_id)
    if folder is None:
        raise ValueError(f"Not an app id: {app_id!r}")
    info = inspect_ipa(ipa)
    folder.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(dir=folder, prefix=".new-"))
    try:
        stored_ipa = temporary / "App.ipa"
        if move:
            shutil.move(str(ipa), str(stored_ipa))
        else:
            shutil.copyfile(ipa, stored_ipa)
        icon = temporary / "icon.png"
        try:
            has_icon = bool(icons.extract_icon(icon_app, stored_ipa, icon)) and icon.is_file()
        except Exception:  # A build without an icon is still a build.
            has_icon = False
        if not has_icon and icon.exists():
            icon.unlink()
        now = _now()
        build = Build(
            key=key, fingerprint=fingerprint, app_id=app_id,
            name=name or info["name"] or (icon_app.name if icon_app else app_id),
            bundle_id=info["bundle_id"], version=info["version"], build=info["build"],
            size=stored_ipa.stat().st_size, built_at=now, last_used_at=now,
            duration=round(duration, 1) if duration is not None else None,
            source=source, configuration=configuration, profile=info["profile"],
            has_icon=has_icon, published=published, path=folder / key,
        )
        _write_json(temporary / "meta.json", build.to_meta())
        with _lock:
            _move_into_place(temporary, build.path)
            prune(app_id, keep, protect=key)
        return build
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def store(app: App, ipa: Path, fingerprint: Fingerprint, duration: float) -> Build:
    """Moves a build of `app` made from `fingerprint` into the store, and prunes the app's builds.

    The app id decides where it goes, whatever the .ipa's bundle id. Raises
    InvalidIPA, leaving `ipa` where it was.
    """
    return _store(app.id, fingerprint.key[:16], ipa, move=True, icon_app=app, keep=app.keep,
                  fingerprint=fingerprint.key, name=None, duration=duration,
                  source=fingerprint.source, configuration=app.configuration, published=False)


def publish(app_id: str, ipa: Path, name: Optional[str] = None) -> Build:
    """Copies an .ipa made elsewhere into the store under `app_id`. Doesn't change the config.

    Its key comes from the file's sha256, so publishing the same file twice
    gives the same build. Raises InvalidIPA.
    """
    digest = hashlib.sha256()
    with open(ipa, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    sha = digest.hexdigest()
    keep = config.DEFAULT_KEEP
    try:
        registered = config.load_config().app(app_id)
    except config.ConfigError:
        registered = None
    if registered is not None:
        keep = registered.keep
    return _store(app_id, sha[:16], Path(ipa), move=False, icon_app=None, keep=keep,
                  fingerprint=sha, name=name, duration=None, source="", configuration=None,
                  published=True)


def mark_used(build: Build) -> None:
    """Records that `build` was just installed, so pruning keeps it. Ignores a build that's gone."""
    with _lock:
        current = _read(build.path)
        if current is None:
            return
        build.last_used_at = current.last_used_at = _now()
        try:
            _write_json(build.path / "meta.json", current.to_meta())
        except OSError:
            pass


def prune(app_id: str, keep: int, protect: Optional[str] = None) -> None:
    """Deletes all but the app's `keep` most recently used builds, never `protect`.

    Also clears temporary folders a stopped store left behind.
    """
    folder = _app_folder(app_id)
    if folder is None:
        return
    with _lock:
        ordered = sorted(list_builds(app_id), key=lambda build: (build.key == protect, build.last_used_at,
                                                                  build.built_at), reverse=True)
        for build in ordered[max(keep, 1):]:
            _remove(build.path)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            if not entry.name.startswith("."):
                continue
            try:
                stale = time.time() - entry.stat(follow_symlinks=False).st_mtime > STALE_AFTER
            except OSError:
                continue
            if stale:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        pass


def delete_all(app_id: str) -> None:
    """Deletes every stored build of the app."""
    folder = _app_folder(app_id)
    if folder is None:
        return
    with _lock:
        _remove(folder)
