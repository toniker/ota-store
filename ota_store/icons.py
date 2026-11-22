"""Finding an app's icon, as a PNG the store page and the install prompt can show.

Tried in order, until one gives a PNG:

1. The app's `icon`: a PNG, an Icon Composer `.icon` (exported with the
   selected Xcode's `ictool`), or an `.appiconset`.
2. A short search of the app's folder for an `.icon`, then an `AppIcon.appiconset`.
3. The loose icon PNGs Xcode copies into the `.app`, named in its Info.plist.
   Those can be Apple's "CgBI" PNGs, which only Apple's decoders read, so
   they're skipped.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from .config import App

MAX_SIZE = 256
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Folders of caches, checkouts and dependencies, which hold other projects' icons.
SKIPPED_FOLDERS = {"build", "DerivedData", "SourcePackages", "Pods", "node_modules", "Carthage", ".build"}
# Bundles and projects never hold the app's icon sources.
SKIPPED_SUFFIXES = (".xcodeproj", ".xcworkspace", ".app", ".appex", ".framework", ".xcframework", ".bundle", ".lproj")
# The search stops after this many folders or this depth below the app's folder,
# so a huge repository can't hold up a build.
SEARCH_FOLDER_LIMIT = 5000
SEARCH_DEPTH_LIMIT = 6
# Path components that mark a folder as another target's: tests, widgets, extensions.
OTHER_TARGET = re.compile(r"(tests?|widgets?|extensions?|intents|watch|clip|preview|stickers|notification)",
                          re.IGNORECASE)
TOOL_TIMEOUT = 60


def extract_icon(app: Optional[App], ipa: Path, dest: Path) -> bool:
    """Writes the icon of the app in `ipa` to `dest`, as a PNG of at most 256 px.

    `app` is None for an .ipa published by hand, which has no sources to look in.
    Returns False, writing nothing, if there's no icon to be found. Never raises
    for a missing or unreadable icon: a build without one is still a build.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="ota-icon-") as folder:
            work = Path(folder)
            for source in _sources(app, Path(ipa), work):
                if _write_resized(source, Path(dest), work):
                    return True
    except Exception:
        pass
    return False


def _sources(app: Optional[App], ipa: Path, work: Path) -> Iterator[Path]:
    """Candidate PNGs, best first, made lazily so the first that works ends the search."""
    if app is not None and app.icon is not None:
        png = _png_from(app.icon, work)
        if png:
            yield png
    if app is not None and app.dir is not None:
        for found in search(app):
            png = _png_from(found, work)
            if png:
                yield png
    png = _png_from_ipa(ipa, work)
    if png:
        yield png


# MARK: - PNGs

def png_size(data: bytes) -> Optional[Tuple[int, int, bool]]:
    """The width and height of PNG `data`, and whether it's a CgBI PNG; None if it isn't a PNG."""
    if not data.startswith(PNG_SIGNATURE):
        return None
    offset, cgbi = len(PNG_SIGNATURE), False
    # CgBI comes before IHDR, so the first two chunks settle both.
    for _ in range(2):
        if len(data) < offset + 8:
            return None
        length, kind = struct.unpack(">I4s", data[offset:offset + 8])
        if kind == b"CgBI":
            cgbi = True
        elif kind == b"IHDR" and length >= 8 and len(data) >= offset + 16:
            width, height = struct.unpack(">II", data[offset + 8:offset + 16])
            return width, height, cgbi
        offset += 12 + length
    return None


def _readable_png(path: Path) -> Optional[Tuple[int, int]]:
    """The size of the PNG at `path` if browsers can show it."""
    try:
        with open(path, "rb") as file:
            info = png_size(file.read(64))
    except OSError:
        return None
    if info is None or info[2]:
        return None
    return info[0], info[1]


def _write_resized(source: Path, dest: Path, work: Path) -> bool:
    """`source` written to `dest` as a PNG no larger than MAX_SIZE, all at once."""
    size = _readable_png(source)
    if size is None:
        return False
    resized = work / "resized.png"
    command = ["/usr/bin/sips", "-s", "format", "png", str(source), "--out", str(resized)]
    if max(size) > MAX_SIZE:
        command[1:1] = ["-Z", str(MAX_SIZE)]
    try:
        made = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL, timeout=TOOL_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        made = False
    if not (made and _readable_png(resized)):
        # Without sips, the original is still better than nothing.
        resized = source
    dest.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=dest.parent, prefix=".icon-", suffix=".png")
    try:
        with os.fdopen(descriptor, "wb") as file, open(resized, "rb") as original:
            file.write(original.read())
        os.replace(temporary, dest)
    except BaseException:
        os.unlink(temporary)
        raise
    return True


# MARK: - Icon sources

def _png_from(path: Path, work: Path) -> Optional[Path]:
    """A PNG made from an icon source: a PNG file, an `.icon` or an `.appiconset`."""
    path = Path(path)
    if path.is_file():
        return path if _readable_png(path) else None
    if not path.is_dir():
        return None
    if path.suffix == ".icon":
        return _export_icon_composer(path, work)
    return _from_icon_set(path)


def ictool() -> Optional[Path]:
    """Icon Composer's command-line tool, from the selected Xcode."""
    developer = os.environ.get("DEVELOPER_DIR")
    if not developer:
        try:
            developer = subprocess.run(["/usr/bin/xcode-select", "-p"], stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, timeout=10).stdout.decode().strip()
        except (OSError, subprocess.SubprocessError):
            developer = ""
    candidates = []
    if developer:
        candidates.append(Path(developer).parent / "Applications")
    candidates.append(Path("/Applications/Xcode.app/Contents/Applications"))
    for applications in candidates:
        tool = applications / "Icon Composer.app" / "Contents" / "Executables" / "ictool"
        if os.access(tool, os.X_OK):
            return tool
    return None


def _export_icon_composer(icon: Path, work: Path) -> Optional[Path]:
    tool = ictool()
    if tool is None:
        return None
    output = work / f"{len(list(work.iterdir()))}-composer.png"
    try:
        subprocess.run([str(tool), str(icon), "--export-image", "--output-file", str(output),
                        "--platform", "iOS", "--rendition", "Default",
                        "--width", "512", "--height", "512", "--scale", "1"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                       timeout=TOOL_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return output if _readable_png(output) else None


def _appearance_rank(image: dict) -> int:
    """0 for the default look, then dark, then tinted or anything else."""
    appearances = image.get("appearances") or []
    if not appearances:
        return 0
    values = {entry.get("value") for entry in appearances if isinstance(entry, dict)}
    return 1 if "dark" in values else 2


def _from_icon_set(folder: Path) -> Optional[Path]:
    """The largest iOS image in an `.appiconset`, the default appearance before dark and tinted."""
    candidates: List[Tuple[int, int, Path]] = []
    try:
        images = json.loads((folder / "Contents.json").read_text()).get("images") or []
    except (OSError, ValueError, AttributeError):
        images = []
    for image in images:
        if not isinstance(image, dict) or not image.get("filename"):
            continue
        if image.get("idiom", "universal") not in ("universal", "iphone", "ipad", "ios-marketing"):
            continue
        if image.get("platform", "ios") != "ios":
            continue
        path = folder / image["filename"]
        size = _readable_png(path)
        if size:
            candidates.append((_appearance_rank(image), -size[0] * size[1], path))
    if not candidates:
        # No usable Contents.json: any PNG in the folder, the dark and tinted ones last.
        for path in sorted(folder.glob("*.png")):
            size = _readable_png(path)
            if size:
                rank = 1 if re.search(r"dark|tint", path.name, re.IGNORECASE) else 0
                candidates.append((rank, -size[0] * size[1], path))
    return min(candidates)[2] if candidates else None


def search(app: App) -> List[Path]:
    """The `.icon` folders, then the `AppIcon.appiconset` folders, in the app's folder.

    Within each kind, ones that look like the app target's come first: those
    under a folder named like the scheme, project or app, then any not under a
    test, widget or extension folder, then the rest, each in the order found.
    """
    if app.dir is None or not app.dir.is_dir():
        return []
    found: List[Tuple[int, int, int, Path]] = []
    preferred = {name.lower() for name in (app.scheme, app.name, app.project.stem if app.project else None) if name}
    queue = [(app.dir, 0)]
    visited = 0
    # Breadth first, so the shallow ones are found before the limit.
    while queue and visited < SEARCH_FOLDER_LIMIT:
        folder, depth = queue.pop(0)
        visited += 1
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name in SKIPPED_FOLDERS or not entry.is_dir(follow_symlinks=False):
                continue
            if name.endswith(".icon") or name == "AppIcon.appiconset":
                path = Path(entry.path)
                parts = path.relative_to(app.dir).parts[:-1]
                if any(part.lower() in preferred for part in parts):
                    rank = 0
                elif any(OTHER_TARGET.search(part) for part in parts):
                    rank = 2
                else:
                    rank = 1
                found.append((0 if name.endswith(".icon") else 1, rank, len(found), path))
            elif not name.endswith(SKIPPED_SUFFIXES) and depth < SEARCH_DEPTH_LIMIT:
                queue.append((Path(entry.path), depth + 1))
    return [path for *_, path in sorted(found)]


# MARK: - The .ipa

def _png_from_ipa(ipa: Path, work: Path) -> Optional[Path]:
    """The largest loose icon PNG in the .ipa's app that isn't CgBI, copied into `work`."""
    try:
        archive = zipfile.ZipFile(ipa)
    except (OSError, zipfile.BadZipFile):
        return None
    with archive:
        names = archive.namelist()
        infos = [name for name in names
                 if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)]
        if not infos:
            return None
        bundle = infos[0][:-len("Info.plist")]
        try:
            info = plistlib.loads(archive.read(infos[0]))
        except Exception:
            return None
        icon_names = _icon_files(info)
        best: Optional[Tuple[int, str]] = None
        for name in names:
            if not name.startswith(bundle) or not name.lower().endswith(".png"):
                continue
            file = name[len(bundle):]
            if "/" in file or not any(file.startswith(icon) for icon in icon_names):
                continue
            with archive.open(name) as member:
                header = png_size(member.read(64))
            if header is None or header[2]:
                continue
            area = header[0] * header[1]
            if best is None or area > best[0]:
                best = (area, name)
        if best is None:
            return None
        output = work / "ipa-icon.png"
        output.write_bytes(archive.read(best[1]))
        return output


def _icon_files(info: dict) -> List[str]:
    """The icon file names Info.plist lists, like `AppIcon60x60`."""
    names: List[str] = []
    for key in ("CFBundleIcons", "CFBundleIcons~ipad"):
        icons = info.get(key)
        primary = icons.get("CFBundlePrimaryIcon") if isinstance(icons, dict) else None
        if isinstance(primary, dict):
            names.extend(str(name) for name in primary.get("CFBundleIconFiles") or [])
    names.extend(str(name) for name in info.get("CFBundleIconFiles") or [])
    if info.get("CFBundleIconFile"):
        names.append(str(info["CFBundleIconFile"]))
    # "Icon.png" is listed with its extension in old plists; it's matched as a prefix.
    return [name[:-4] if name.lower().endswith(".png") else name for name in names if name]
