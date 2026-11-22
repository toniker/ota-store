"""What an app's next build would be made from, as one hash.

A fingerprint's key covers the builder (its command, or the built-in builder
and its version, plus project, scheme and configuration), the selected Xcode,
and each of the app's roots: `app.dir`, then `app.inputs`. Two taps with the
same key would build the same thing, so the store can reuse the stored build.

A root in a git work tree is the tree id that folder would have if its
working tree was committed: the tree at HEAD (so commits elsewhere in a
monorepo don't count) with each path `git status` reports under it replaced by
what's on disk, as git would store it (content and mode). Unchanged folders
keep their HEAD ids, so only the folders on the way to a change are read. The
same files give the same key whether they're committed or not. Submodules
listed in .gitmodules count by their checkout's tree, worked out the same way,
rather than by commit. Outside git, a root is a walk of its paths, sizes,
modification times and executable bits. A root that's a file is hashed by
content and executable bit.

Git runs with `--no-optional-locks`: the store runs next to agents using the
same repositories, and must never write their index.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from . import xcode_build
from .config import App

# Part of every key: bump it when the way keys are made changes.
FORMAT_VERSION = 2
GIT_TIMEOUT = 10.0
# Not part of a build, and rewritten all the time by Xcode or Finder.
WALK_SKIP = frozenset({".git", "xcuserdata", "DerivedData", "build", ".build", ".DS_Store"})
# Inherited from a git hook or similar, these would point git at another repository.
GIT_ENVIRONMENT_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX",
                        "GIT_NAMESPACE")
XCODE_SELECT_LINK = "/var/db/xcode_select_link"
DELETED = "deleted"


class FingerprintError(Exception):
    """The app's sources can't be read, e.g. its folder is gone."""


@dataclass(frozen=True)
class Fingerprint:
    # sha256, in hex, of everything that goes into a build. Two equal keys
    # mean the build would come out the same.
    key: str
    # Where the sources are at, for people: "main@f2d6080", "main@f2d6080+changes",
    # or "" outside git.
    source: str
    # Whether the app's folders have uncommitted changes.
    dirty: bool


@dataclass(frozen=True)
class Commit:
    sha: str      # short
    subject: str
    date: str     # ISO 8601, with its offset


# MARK: - Git

class _NotGit(Exception):
    """The folder isn't in a git work tree, or git isn't there to ask."""


def _git_environment() -> Dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key not in GIT_ENVIRONMENT_DROP}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_LITERAL_PATHSPECS"] = "1"
    return environment


def _git(cwd: Path, *arguments: str, input: Optional[bytes] = None, check: bool = True) -> subprocess.CompletedProcess:
    """Runs git read-only in `cwd`. Raises FingerprintError if it fails (with `check`) or takes too long."""
    command = ["git", "--no-optional-locks", "-c", "core.quotePath=false", *arguments]
    try:
        result = subprocess.run(command, cwd=str(cwd), input=input, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=_git_environment(), timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise FingerprintError(f"git {arguments[0]} took over {GIT_TIMEOUT:g}s in {cwd}") from error
    except OSError as error:
        raise _NotGit(str(error)) from error
    if check and result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip() or f"exit status {result.returncode}"
        raise FingerprintError(f"git {arguments[0]} failed in {cwd}: {message}")
    return result


@dataclass(frozen=True)
class _Repository:
    top: Path
    prefix: str             # the folder's path from the top, "" or ending in "/"
    head: Optional[str]     # the full commit id, None before the first commit
    tree: Optional[str]     # the folder's tree at HEAD, None if it isn't in HEAD
    branch: str             # "HEAD" when detached


def _repository(folder: Path) -> _Repository:
    """The work tree `folder` is in. Raises _NotGit outside one."""
    # Everything in one process when there's a HEAD with this folder in it, as there usually is.
    result = _git(folder, "rev-parse", "--show-toplevel", "--show-prefix", "HEAD", "HEAD:./",
                  "--abbrev-ref", "HEAD", "--", check=False)
    lines = result.stdout.decode("utf-8", "surrogateescape").split("\n")
    if result.returncode == 0 and len(lines) >= 5 and lines[0]:
        return _Repository(top=Path(lines[0]), prefix=lines[1], head=lines[2], tree=lines[3], branch=lines[4])

    result = _git(folder, "rev-parse", "--show-toplevel", "--show-prefix", check=False)
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace")
        if "not a git repository" in message.lower():
            raise _NotGit(message)
        raise FingerprintError(f"git can't read {folder}: {message.strip()}")
    lines = result.stdout.decode("utf-8", "surrogateescape").split("\n")
    if len(lines) < 2 or not lines[0]:
        # Inside a .git folder, or a bare repository: there's no work tree to fingerprint.
        raise _NotGit(f"{folder} isn't in a work tree")
    top, prefix = Path(lines[0]), lines[1]
    head, tree = _objects(top, ["HEAD", f"HEAD:{prefix.rstrip('/')}" if prefix else "HEAD^{tree}"])
    branch = _git(top, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.decode().strip()
    return _Repository(top=top, prefix=prefix, head=head, tree=tree, branch=branch or "HEAD")


def _objects(top: Path, names: Sequence[str]) -> List[Optional[str]]:
    """The object id each of `names` (like "HEAD" or "HEAD:ios") resolves to, None where it doesn't."""
    # cat-file reads one name per line; a path with a newline in it counts as not found.
    asked = [name for name in names if "\n" not in name]
    if not asked:
        return [None] * len(names)
    stdin = "".join(f"{name}\n" for name in asked).encode("utf-8", "surrogateescape")
    output = _git(top, "cat-file", "--batch-check=%(objectname)", input=stdin).stdout.decode()
    # "<name> missing" (or "ambiguous") when it doesn't resolve.
    found = dict(zip(asked, (None if " " in line or not line else line for line in output.split("\n"))))
    return [found.get(name) for name in names]


def _status(repository: _Repository) -> List[str]:
    """The paths, from the top, that `git status` reports under the folder: changed, staged, deleted or untracked."""
    arguments = ["status", "--porcelain", "-z", "--untracked-files=all", "--ignore-submodules=none"]
    if repository.prefix:
        arguments += ["--", repository.prefix]
    output = _git(repository.top, *arguments).stdout.decode("utf-8", "surrogateescape")
    fields = output.split("\0")
    paths: List[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            # A rename's origin follows it; it's gone from where it was.
            if index < len(fields):
                paths.append(fields[index])
            index += 1
    return sorted({path for path in paths if path.startswith(repository.prefix)})


def _hash_files(top: Path, paths: Sequence[str], algorithm: str = "sha1") -> Dict[str, str]:
    """Blob ids of the working-tree files at `paths` (from the top), as they are on disk."""
    hashes: Dict[str, str] = {}
    # hash-object reads one path per line and unquotes a leading quote; absolute
    # paths never start with one, and paths with newlines are hashed here.
    batch = [path for path in paths if "\n" not in path]
    if batch:
        stdin = "".join(f"{top / path}\n" for path in batch).encode("utf-8", "surrogateescape")
        result = _git(top, "hash-object", "--no-filters", "--stdin-paths", input=stdin, check=False)
        ids = result.stdout.decode().split()
        if result.returncode == 0 and len(ids) == len(batch):
            hashes.update(zip(batch, ids))
    for path in paths:
        if path not in hashes:
            # A file that changed or went away while git was reading it.
            try:
                hashes[path] = _object_id("blob", (top / path).read_bytes(), algorithm)
            except FileNotFoundError:
                hashes[path] = DELETED
            except OSError as error:
                raise FingerprintError(f"Can't read {top / path}: {error.strerror}") from error
    return hashes


# Git tree entry modes.
TREE_MODE = "40000"
GITLINK_MODE = "160000"
LINK_MODE = "120000"
FILE_MODES = ("100644", "100755")


def _algorithm(repository: _Repository) -> str:
    known = repository.head or repository.tree or ""
    return "sha256" if len(known) == 64 else "sha1"


def _object_id(kind: str, data: bytes, algorithm: str = "sha1") -> str:
    """The id git gives an object of `kind` ("blob", "tree") holding `data`."""
    return hashlib.new(algorithm, b"%s %d\0" % (kind.encode(), len(data)) + data).hexdigest()


def _empty_tree(algorithm: str) -> str:
    return _object_id("tree", b"", algorithm)


def _trees(top: Path, names: Sequence[str]) -> Dict[str, Optional[bytes]]:
    """The raw content of each tree object `names` (like "<tree>:ios/App") resolves to, None where it isn't a tree."""
    asked = sorted({name for name in names if "\n" not in name})
    found: Dict[str, Optional[bytes]] = {name: None for name in names}
    if not asked:
        return found
    stdin = "".join(f"{name}\n" for name in asked).encode("utf-8", "surrogateescape")
    output = _git(top, "cat-file", "--batch", input=stdin).stdout
    position = 0
    for name in asked:
        newline = output.find(b"\n", position)
        if newline < 0:
            break
        header = output[position:newline].split(b" ")
        position = newline + 1
        # "<id> <type> <size>", then the content and a newline; or "<name> missing".
        if len(header) == 3 and header[2].isdigit() and header[1] in (b"tree", b"blob", b"commit", b"tag"):
            size = int(header[2])
            if header[1] == b"tree":
                found[name] = output[position:position + size]
            position += size + 1
    return found


def _tree_entries(data: bytes, size: int) -> Dict[str, Tuple[str, str]]:
    """name -> (mode, id in hex) for each entry of a raw tree object whose ids are `size` bytes."""
    entries: Dict[str, Tuple[str, str]] = {}
    position = 0
    while position < len(data):
        space = data.index(b" ", position)
        nul = data.index(b"\0", space)
        mode = data[position:space].decode()
        name = data[space + 1:nul].decode("utf-8", "surrogateescape")
        entries[name] = (mode, data[nul + 1:nul + 1 + size].hex())
        position = nul + 1 + size
    return entries


class _Node:
    """A folder in the overlay: what changed in it, by name."""
    __slots__ = ("children", "change", "submodule")

    def __init__(self) -> None:
        self.children: Dict[str, _Node] = {}
        # What's on disk at this path, when git status reported it (see _working).
        self.change: Optional[Tuple] = None
        # Listed in .gitmodules: a gitlink here stands for its checkout's content.
        self.submodule = False


def _node(root: _Node, path: str) -> _Node:
    node = root
    for name in path.split("/"):
        node = node.children.setdefault(name, _Node())
    return node


@dataclass(frozen=True)
class _GitState:
    repository: _Repository
    # The canonical tree of the folder as it is on disk: the id git would give
    # the folder's tree if everything was committed, so it's the HEAD tree when
    # nothing changed. Submodules listed in .gitmodules count by their
    # checkout's tree rather than their commit, so it isn't always a git object.
    tree: str
    # Whether anything under the folder differs from HEAD.
    dirty: bool


def _git_state(repository: _Repository, known_clean: bool = False) -> _GitState:
    """The folder's canonical tree. `known_clean` skips git status, for a submodule the superproject says is clean."""
    algorithm = _algorithm(repository)
    prefix = repository.prefix
    paths = [] if known_clean else _status(repository)
    modules = _submodule_paths(repository)
    if not paths and not modules:
        return _GitState(repository, repository.tree or _empty_tree(algorithm), False)

    root = _Node()
    for path in modules:
        _node(root, path).submodule = True
    files: List[str] = []
    for path in paths:
        relative = path[len(prefix):].rstrip("/")
        if not relative:
            continue
        node = _node(root, relative)
        location = repository.top / path
        try:
            mode = os.lstat(location).st_mode
        except FileNotFoundError:
            node.change = ("deleted",)
            continue
        except OSError as error:
            raise FingerprintError(f"Can't read {location}: {error.strerror}") from error
        if stat.S_ISLNK(mode):
            node.change = ("entry", LINK_MODE, _object_id("blob", os.fsencode(os.readlink(location)), algorithm))
        elif stat.S_ISDIR(mode):
            # A submodule or a nested repository: git status doesn't look inside.
            node.change = _nested(location)
        else:
            node.change = ("file", bool(mode & stat.S_IXUSR), path)
            files.append(path)
    blobs = _hash_files(repository.top, files, algorithm)
    file_mode = _file_mode(repository) if files else True

    # The folders on the way to each change, as they are at HEAD, in one cat-file.
    listings: Dict[str, Optional[bytes]] = {}
    if repository.tree is not None:
        folders = {""}
        for path in [*modules, *(path[len(prefix):].rstrip("/") for path in paths)]:
            parts = path.split("/")[:-1]
            folders.update("/".join(parts[:count]) + "/" for count in range(1, len(parts) + 1))
        names = {folder: f"{repository.tree}:{folder.rstrip('/')}" if folder else repository.tree
                 for folder in folders}
        fetched = _trees(repository.top, list(names.values()))
        listings = {folder: fetched[name] for folder, name in names.items() if "\n" not in name}

    dirty = False

    def entry_for(node: _Node, path: str, before: Optional[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
        """What goes in the tree for a reported or submodule path, with `before` its HEAD entry."""
        nonlocal dirty
        change = node.change
        if change is None:
            # A submodule git status didn't report: checked out at the commit HEAD has.
            if before is not None and before[0] == GITLINK_MODE:
                return _checkout_tree(repository.top / prefix / path) or before
            return before
        kind = change[0]
        if kind == "deleted":
            now, compare = None, None
        elif kind == "file":
            mode = FILE_MODES[1] if change[1] else FILE_MODES[0]
            if not file_mode:
                # core.fileMode is off: git keeps the mode it has.
                mode = before[0] if before is not None and before[0] in FILE_MODES else FILE_MODES[0]
            blob = blobs[change[2]]
            now = compare = None if blob == DELETED else (mode, blob)
        elif kind == "entry":
            now = compare = (change[1], change[2])
        else:
            # A nested repository: ("repo", commit when clean or None, its canonical tree) or ("walk", digest).
            if kind == "repo" and change[1] is not None:
                compare = (GITLINK_MODE, change[1])
                now = (TREE_MODE, change[2]) if node.submodule else compare
            elif kind == "repo":
                compare, now = ("dirty",), (TREE_MODE, change[2])
            else:
                compare, now = ("dirty",), ("walk", change[1])
        if compare != before:
            dirty = True
        return now

    def build(folder: str, tree: Optional[str], node: _Node) -> Optional[str]:
        """The canonical tree of `folder` (from the root, "" or ending in "/"), None when it ends up empty."""
        data = listings.get(folder)
        if data is None and tree is not None:
            data = _trees(repository.top, [tree])[tree]
        before = _tree_entries(data, hashlib.new(algorithm).digest_size) if data is not None else {}
        entries = dict(before)
        for name, child in node.children.items():
            path = folder + name
            previous = before.get(name)
            now: Optional[Tuple[str, str]] = previous
            if child.change is not None or child.submodule:
                now = entry_for(child, path, previous)
            if child.children:
                inside = previous[1] if previous is not None and previous[0] == TREE_MODE else None
                if child.change is not None and now is not None and now[0] != TREE_MODE:
                    inside = None
                subtree = build(path + "/", inside, child)
                now = (TREE_MODE, subtree) if subtree is not None else (now if now and now[0] != TREE_MODE else None)
            if now is None:
                entries.pop(name, None)
            else:
                entries[name] = now
        if tree is not None and entries == before:
            return tree
        if not entries:
            return None
        return _object_id("tree", _tree_data(entries), algorithm)

    tree = build("", repository.tree, root)
    return _GitState(repository, tree or _empty_tree(algorithm), dirty)


def _tree_data(entries: Dict[str, Tuple[str, str]]) -> bytes:
    """A tree object's content, ordered as git orders it: folders as if their names ended in "/"."""
    def order(name: str) -> bytes:
        encoded = name.encode("utf-8", "surrogateescape")
        return encoded + b"/" if entries[name][0] == TREE_MODE else encoded
    data = bytearray()
    for name in sorted(entries, key=order):
        mode, value = entries[name]
        identifier = bytes.fromhex(value) if mode in (TREE_MODE, GITLINK_MODE, LINK_MODE, *FILE_MODES) \
            else hashlib.sha1(value.encode()).digest()
        data += b"%s %s\0%s" % (mode.encode(), name.encode("utf-8", "surrogateescape"), identifier)
    return bytes(data)


def _file_mode(repository: _Repository) -> bool:
    """core.fileMode: whether git looks at the executable bit."""
    result = _git(repository.top, "config", "--bool", "core.fileMode", check=False)
    return result.stdout.decode().strip() != "false"


def _submodule_paths(repository: _Repository) -> List[str]:
    """The paths, from the folder, that .gitmodules lists under it."""
    modules = repository.top / ".gitmodules"
    if not modules.is_file():
        return []
    result = _git(repository.top, "config", "-z", "-f", str(modules), "--get-regexp", r"^submodule\..*\.path$",
                  check=False)
    paths = []
    for record in result.stdout.decode("utf-8", "surrogateescape").split("\0"):
        _, _, path = record.partition("\n")
        path = path.strip("/")
        if path and path.startswith(repository.prefix) and path != repository.prefix.rstrip("/"):
            paths.append(path[len(repository.prefix):])
    return paths


def _checkout(folder: Path) -> Optional[_Repository]:
    """The repository whose work tree is exactly `folder`, None if there isn't one."""
    try:
        repository = _repository(folder)
    except _NotGit:
        return None
    if os.path.realpath(repository.top) != os.path.realpath(folder):
        return None
    return repository


def _checkout_tree(folder: Path) -> Optional[Tuple[str, str]]:
    """The entry for a submodule git status says is clean: its checkout's canonical tree."""
    if not os.path.lexists(folder / ".git"):
        return None  # not checked out
    repository = _checkout(folder)
    if repository is None or repository.head is None:
        return None
    return (TREE_MODE, _git_state(repository, known_clean=True).tree)


def _nested(folder: Path) -> Tuple:
    """What a submodule or nested repository holds: ("repo", commit when clean else None, canonical tree), or a walk."""
    repository = _checkout(folder)
    if repository is None:
        return ("walk", _digest(_walk(folder)))
    state = _git_state(repository)
    clean = not state.dirty and repository.head is not None
    return ("repo", repository.head if clean else None, state.tree)


def _git_lines(state: _GitState) -> Iterator[str]:
    yield f"tree {state.tree}"


# MARK: - Outside git

def _walk(root: Path) -> Iterator[str]:
    """Each path under `root`, with its size and modification time, in a stable order."""
    def visit(folder: str, relative: str) -> Iterator[str]:
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            if entry.name in WALK_SKIP:
                continue
            path = f"{relative}{entry.name}"
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                yield f"dir {path}"
                yield from visit(entry.path, path + "/")
            elif stat.S_ISLNK(info.st_mode):
                yield f"link {path}\0{os.readlink(entry.path)}"
            else:
                yield f"file {path}\0{info.st_size}\0{info.st_mtime_ns}\0{_executable(info.st_mode)}"
    return visit(str(root), "")


def _executable(mode: int) -> str:
    """"x" when the owner may run it, as git sees the executable bit, else "-"."""
    return "x" if mode & stat.S_IXUSR else "-"


def _file_lines(path: Path) -> Iterator[str]:
    digest = hashlib.sha256()
    try:
        mode = os.stat(path).st_mode
        with open(path, "rb") as file:
            for block in iter(lambda: file.read(1 << 20), b""):
                digest.update(block)
    except OSError as error:
        raise FingerprintError(f"Can't read {path}: {error.strerror}") from error
    yield f"file {digest.hexdigest()} {_executable(mode)}"


def _digest(lines: Iterator[str]) -> str:
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode("utf-8", "surrogateescape"))
        digest.update(b"\n")
    return digest.hexdigest()


# MARK: - Xcode

_xcode_cache: Dict[Tuple[str, int], str] = {}


def developer_dir() -> Optional[str]:
    """The selected Xcode's Developer folder: DEVELOPER_DIR, else what `xcode-select -p` says."""
    selected = os.environ.get("DEVELOPER_DIR")
    if selected:
        return selected
    try:
        # What xcode-select -p reads, without starting a process.
        return os.readlink(XCODE_SELECT_LINK)
    except OSError:
        pass
    try:
        result = subprocess.run(["xcode-select", "-p"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.decode().strip() or None


def xcode_identity(developer: Optional[str] = None) -> str:
    """The selected Xcode, as "Xcode <version> (<build>)", or its folder when it has no version.plist.

    Reads `<developer dir>/../version.plist`, cached per path and modification time.
    """
    developer = developer if developer is not None else developer_dir()
    if not developer:
        return "none"
    plist = os.path.normpath(os.path.join(developer, "..", "version.plist"))
    try:
        modified = os.stat(plist).st_mtime_ns
    except OSError:
        # Only the Command Line Tools, or an unusual layout.
        return f"path {os.path.normpath(developer)}"
    cached = _xcode_cache.get((plist, modified))
    if cached is not None:
        return cached
    try:
        with open(plist, "rb") as file:
            info = plistlib.load(file)
        identity = f"Xcode {info.get('CFBundleShortVersionString', '?')} ({info.get('ProductBuildVersion', '?')})"
    except Exception:
        identity = f"path {os.path.normpath(developer)}"
    _xcode_cache[(plist, modified)] = identity
    return identity


# MARK: - Fingerprints

def _builder_lines(app: App) -> Iterator[str]:
    if app.command:
        yield f"command {app.command}"
    else:
        yield f"built-in {xcode_build.BUILDER_VERSION}"
    project = ""
    if app.project is not None:
        project = str(app.project)
        if app.dir is not None:
            # Relative, so a moved checkout keeps its builds.
            try:
                project = str(app.project.relative_to(app.dir))
            except ValueError:
                pass
    yield f"project {project}"
    yield f"scheme {app.scheme or ''}"
    yield f"configuration {app.configuration}"


def compute(app: App, xcode: Optional[str] = None) -> Fingerprint:
    """The fingerprint of `app` as its files are now. Raises FingerprintError.

    `xcode` stands in for the selected Xcode's identity (see `xcode_identity`),
    for tests and for callers that have already read it.
    """
    if app.dir is None:
        raise FingerprintError(f"{app.id}: has no folder to build from")
    lines = [f"ota-store fingerprint {FORMAT_VERSION}", *_builder_lines(app),
             f"xcode {xcode if xcode is not None else xcode_identity()}"]
    source = ""
    dirty = False
    for index, root in enumerate([app.dir, *app.inputs]):
        kind = "folder" if index == 0 else "input"
        if not os.path.lexists(root):
            raise FingerprintError(f"{app.id}: {kind} not found: {root}")
        if not root.is_dir():
            lines.append(f"root {index} file")
            lines.extend(_file_lines(root))
            continue
        try:
            repository = _repository(root)
        except _NotGit:
            lines.append(f"root {index} walk")
            lines.extend(_walk(root))
            continue
        state = _git_state(repository)
        dirty = dirty or state.dirty
        lines.append(f"root {index} git")
        lines.extend(_git_lines(state))
        if index == 0:
            source = _source(repository)
    if source and dirty:
        source += "+changes"
    return Fingerprint(key=_digest(iter(lines)), source=source, dirty=dirty)


def _source(repository: _Repository) -> str:
    """"branch@shortsha", "HEAD@shortsha" when detached, or the branch alone before the first commit."""
    if repository.head is None:
        return repository.branch
    short = _git(repository.top, "rev-parse", "--short", repository.head).stdout.decode().strip()
    return f"{repository.branch}@{short}"


def recent_commits(app: App, limit: int = 5) -> List[Commit]:
    """The latest commits touching the app's folder, newest first. [] outside git or if git fails."""
    if app.dir is None or not app.dir.is_dir() or limit < 1:
        return []
    try:
        repository = _repository(app.dir)
        if repository.head is None:
            return []
        arguments = ["-c", "log.showSignature=false", "log", "-z", f"-n{limit}",
                     "--format=%h%x1f%s%x1f%cI", "HEAD"]
        if repository.prefix:
            arguments += ["--", repository.prefix]
        output = _git(repository.top, *arguments).stdout.decode("utf-8", "replace")
    except (_NotGit, FingerprintError):
        return []
    commits = []
    for record in output.split("\0"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) == 3:
            commits.append(Commit(sha=parts[0], subject=parts[1], date=parts[2]))
    return commits
