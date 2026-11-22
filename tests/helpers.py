"""Shared test helpers: temporary store folders, fake .ipa files and PNGs.

Tests run with `/usr/bin/python3 -m unittest discover -s tests -t .` from the
repository root, so `ota_store` and `tests` import as packages.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import struct
import sys
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent
FAKE_BUILD = Path(__file__).resolve().parent / "fake_build.py"


@contextlib.contextmanager
def temp_store() -> Iterator[Path]:
    """Points OTA_STORE_HOME and OTA_STORE_CACHE at a new temporary folder for the duration."""
    saved = {key: os.environ.get(key) for key in ("OTA_STORE_HOME", "OTA_STORE_CACHE")}
    with tempfile.TemporaryDirectory(prefix="ota-store-test-") as folder:
        root = Path(folder).resolve()
        os.environ["OTA_STORE_HOME"] = str(root / "home")
        os.environ["OTA_STORE_CACHE"] = str(root / "cache")
        try:
            yield root
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def png(width: int = 1, height: int = 1, rgb=(74, 120, 194)) -> bytes:
    """A valid, solid-colour PNG."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgb) * width
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height))
            + chunk(b"IEND", b""))


def mobileprovision(expires: datetime, devices: int = 2, name: str = "iOS Team Provisioning Profile: *",
                    team: str = "Example Team") -> bytes:
    """A stand-in for embedded.mobileprovision: a plist wrapped in bytes, as the real CMS envelope is."""
    profile = {
        "Name": name,
        "TeamName": team,
        "TeamIdentifier": ["ABCDE12345"],
        "CreationDate": expires - timedelta(days=365),
        "ExpirationDate": expires,
        "ProvisionedDevices": [f"00008110-{index:016X}" for index in range(devices)],
        "Entitlements": {"get-task-allow": True},
    }
    return b"0\x82\x1f\x00\x06\x09*\x86H\x86\xf7\r\x01\x07\x02" + plistlib.dumps(profile) + b"\x00\xa0\x82\x0b\x00"


def make_ipa(path: Path, bundle_id: str = "com.example.Fake", name: str = "Fake", version: str = "1.0",
             build: str = "1", expires: Optional[datetime] = None, devices: int = 2,
             icon: Optional[bytes] = None, profile: bool = True) -> Path:
    """Writes a minimal .ipa: Payload/<name>.app with an Info.plist, a profile and, optionally, an icon.

    `expires` defaults to a year from now. `icon` is PNG data, stored as the
    loose AppIcon60x60@2x.png that Xcode puts in app bundles.
    """
    import zipfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    app = f"Payload/{name}.app"
    info = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleName": name,
        "CFBundleDisplayName": name,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": build,
        "CFBundleExecutable": name,
    }
    if icon is not None:
        info["CFBundleIcons"] = {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["AppIcon60x60"], "CFBundleIconName": "AppIcon"}}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{app}/Info.plist", plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
        archive.writestr(f"{app}/{name}", b"\xcf\xfa\xed\xfe fake executable")
        if profile:
            archive.writestr(f"{app}/embedded.mobileprovision",
                             mobileprovision(expires or datetime.now(timezone.utc) + timedelta(days=365), devices))
        if icon is not None:
            archive.writestr(f"{app}/AppIcon60x60@2x.png", icon)
    return path


def fake_build_command(**options: str) -> str:
    """A build command for an App's `command` that runs tests/fake_build.py.

    Options become FAKE_* environment variables for it, e.g.
    `fake_build_command(sleep="0.5", fail="1")`; see fake_build.py.
    """
    assignments = " ".join(f"FAKE_{key.upper()}={value!s}" for key, value in options.items())
    return f"{assignments} {sys.executable} {FAKE_BUILD}".strip()
