"""Tests for ota_store.fingerprint, on temporary git repositories and folders."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ota_store import fingerprint, xcode_build
from ota_store.config import App
from ota_store.fingerprint import FingerprintError, compute, recent_commits, xcode_identity

XCODE = "Xcode 1.0 (1A1)"


def git(folder: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main",
                             "-c", "commit.gpgSign=false", *arguments],
                            cwd=str(folder), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return result.stdout.decode()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def commit(repository: Path, message: str = "change") -> None:
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", message)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="ota-fingerprint-")).resolve()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def repository(self, name: str = "repo") -> Path:
        """A monorepo with the app in ios/ and something else in backend/, one commit in."""
        top = self.folder / name
        top.mkdir()
        git(top, "init", "-q")
        write(top / ".gitignore", "*.log\n")
        write(top / "ios" / "App.swift", "print(1)\n")
        write(top / "ios" / "Info.plist", "<plist/>\n")
        write(top / "backend" / "server.py", "pass\n")
        commit(top, "initial")
        return top

    def app(self, folder: Path, **options) -> App:
        return App(id="app", name="App", dir=folder, **options)

    def key(self, app: App, xcode: str = XCODE) -> str:
        return compute(app, xcode=xcode).key


class GitTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.top = self.repository()
        self.ios = self.top / "ios"
        self.subject = self.app(self.ios)
        self.original = self.key(self.subject)

    def test_unchanged_is_the_same(self) -> None:
        self.assertEqual(self.key(self.subject), self.original)
        self.assertFalse(compute(self.subject, xcode=XCODE).dirty)

    def test_tracked_edit_changes_it_and_reverting_restores_it(self) -> None:
        write(self.ios / "App.swift", "print(2)\n")
        edited = self.key(self.subject)
        self.assertNotEqual(edited, self.original)
        self.assertTrue(compute(self.subject, xcode=XCODE).dirty)
        write(self.ios / "App.swift", "print(3)\n")
        self.assertNotEqual(self.key(self.subject), edited)
        write(self.ios / "App.swift", "print(1)\n")
        self.assertEqual(self.key(self.subject), self.original)

    def test_untracked_file_changes_it(self) -> None:
        write(self.ios / "New.swift", "new\n")
        self.assertNotEqual(self.key(self.subject), self.original)

    def test_ignored_file_does_not(self) -> None:
        write(self.ios / "build.log", "noise\n")
        self.assertEqual(self.key(self.subject), self.original)

    def test_changes_outside_the_root_do_not_count(self) -> None:
        write(self.top / "backend" / "server.py", "edited\n")
        write(self.top / "backend" / "new.py", "new\n")
        self.assertEqual(self.key(self.subject), self.original)
        commit(self.top, "backend only")
        self.assertEqual(self.key(self.subject), self.original)

    def test_commit_inside_the_root_changes_it(self) -> None:
        write(self.ios / "App.swift", "print(2)\n")
        dirty = self.key(self.subject)
        commit(self.top)
        committed = self.key(self.subject)
        self.assertNotEqual(committed, self.original)
        self.assertFalse(compute(self.subject, xcode=XCODE).dirty)
        # The same content, committed or not: the build would be the same.
        self.assertEqual(committed, dirty)

    def test_deletion_changes_it(self) -> None:
        (self.ios / "Info.plist").unlink()
        deleted = self.key(self.subject)
        self.assertNotEqual(deleted, self.original)
        write(self.ios / "Info.plist", "")
        self.assertNotEqual(self.key(self.subject), deleted)
        write(self.ios / "Info.plist", "<plist/>\n")
        self.assertEqual(self.key(self.subject), self.original)

    def test_staged_and_unstaged_count_by_content(self) -> None:
        write(self.ios / "App.swift", "print(2)\n")
        unstaged = self.key(self.subject)
        git(self.top, "add", "ios/App.swift")
        self.assertEqual(self.key(self.subject), unstaged)
        # Staged, then undone in the working tree: the build would be the original one.
        write(self.ios / "App.swift", "print(1)\n")
        self.assertEqual(self.key(self.subject), self.original)

    def test_staged_deletion_and_rename(self) -> None:
        git(self.top, "rm", "-q", "--cached", "ios/Info.plist")
        self.assertEqual(self.key(self.subject), self.original)
        git(self.top, "mv", "ios/App.swift", "ios/Main.swift")
        renamed = self.key(self.subject)
        self.assertNotEqual(renamed, self.original)
        os.rename(self.ios / "Main.swift", self.ios / "App.swift")
        git(self.top, "add", "-A")
        self.assertEqual(self.key(self.subject), self.original)

    def test_does_not_write_the_index(self) -> None:
        index = self.top / ".git" / "index"
        before = index.read_bytes()
        time.sleep(0.01)
        # A touched file makes git status want to refresh the index.
        os.utime(self.ios / "App.swift")
        write(self.ios / "New.swift", "new\n")
        compute(self.subject, xcode=XCODE)
        recent_commits(self.subject)
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse((self.top / ".git" / "index.lock").exists())

    def test_whole_repository_as_root(self) -> None:
        whole = self.app(self.top)
        original = self.key(whole)
        write(self.top / "backend" / "server.py", "edited\n")
        self.assertNotEqual(self.key(whole), original)

    def test_spaces_and_unicode_in_paths(self) -> None:
        odd = self.ios / "Ünïcode folder" / "Fïle with spaces.swift"
        write(odd, "one\n")
        untracked = self.key(self.subject)
        self.assertNotEqual(untracked, self.original)
        write(odd, "two\n")
        self.assertNotEqual(self.key(self.subject), untracked)
        commit(self.top)
        committed = self.key(self.subject)
        write(odd, "three\n")
        self.assertNotEqual(self.key(self.subject), committed)
        write(odd, "two\n")
        self.assertEqual(self.key(self.subject), committed)
        odd.unlink()
        self.assertNotEqual(self.key(self.subject), committed)

    def test_path_with_a_newline_and_a_quote(self) -> None:
        odd = write(self.ios / 'line\nbreak "quoted".swift', "one\n")
        first = self.key(self.subject)
        self.assertNotEqual(first, self.original)
        write(odd, "two\n")
        self.assertNotEqual(self.key(self.subject), first)
        odd.unlink()
        self.assertEqual(self.key(self.subject), self.original)

    def test_root_that_is_a_new_folder(self) -> None:
        new = write(self.top / "watch" / "Watch.swift", "watch\n").parent
        app = self.app(new)
        first = self.key(app)
        write(new / "Watch.swift", "edited\n")
        self.assertNotEqual(self.key(app), first)

    def test_nested_repository(self) -> None:
        nested = self.ios / "Vendor"
        nested.mkdir()
        git(nested, "init", "-q")
        write(nested / "Lib.swift", "lib\n")
        empty = self.key(self.subject)
        self.assertNotEqual(empty, self.original)
        commit(nested)
        committed = self.key(self.subject)
        self.assertNotEqual(committed, empty)
        write(nested / "Lib.swift", "edited\n")
        self.assertNotEqual(self.key(self.subject), committed)
        write(nested / "Lib.swift", "lib\n")
        self.assertEqual(self.key(self.subject), committed)

    def test_submodule(self) -> None:
        library = self.folder / "library"
        library.mkdir()
        git(library, "init", "-q")
        write(library / "Lib.swift", "lib\n")
        commit(library)
        git(self.top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(library), "ios/Lib")
        commit(self.top, "add submodule")
        clean = self.key(self.subject)
        self.assertFalse(compute(self.subject, xcode=XCODE).dirty)
        write(self.ios / "Lib" / "Lib.swift", "edited\n")
        self.assertNotEqual(self.key(self.subject), clean)
        self.assertTrue(compute(self.subject, xcode=XCODE).dirty)
        write(self.ios / "Lib" / "Lib.swift", "lib\n")
        self.assertEqual(self.key(self.subject), clean)
        write(self.ios / "Lib" / "More.swift", "more\n")
        commit(self.ios / "Lib", "moved on")
        moved = self.key(self.subject)
        self.assertNotEqual(moved, clean)
        self.assertTrue(compute(self.subject, xcode=XCODE).dirty)
        # Committing the new pointer doesn't change what's checked out.
        commit(self.top, "update submodule")
        self.assertEqual(self.key(self.subject), moved)
        self.assertFalse(compute(self.subject, xcode=XCODE).dirty)

    def add_submodule(self) -> Path:
        library = self.folder / "library"
        library.mkdir()
        git(library, "init", "-q")
        write(library / "Lib.swift", "lib\n")
        commit(library)
        git(self.top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(library), "ios/Lib")
        commit(self.top, "add submodule")
        return self.ios / "Lib"

    def test_submodule_counts_by_content(self) -> None:
        submodule = self.add_submodule()
        clean = self.key(self.subject)
        # Uncommitted inside the submodule, then committed there: the same files.
        write(submodule / "Lib.swift", "edited\n")
        edited = self.key(self.subject)
        self.assertNotEqual(edited, clean)
        commit(submodule, "edit")
        self.assertEqual(self.key(self.subject), edited)
        commit(self.top, "update submodule")
        self.assertEqual(self.key(self.subject), edited)
        # Back to the first commit, with the superproject still pointing at the second.
        git(submodule, "checkout", "-q", "HEAD~1")
        self.assertEqual(self.key(self.subject), clean)
        self.assertTrue(compute(self.subject, xcode=XCODE).dirty)

    def test_uninitialized_submodule(self) -> None:
        submodule = self.add_submodule()
        clean = self.key(self.subject)
        git(self.top, "submodule", "deinit", "-q", "-f", "ios/Lib")
        self.assertNotEqual(self.key(self.subject), clean)
        self.assertFalse(compute(self.subject, xcode=XCODE).dirty)

    def test_same_content_committed_or_not(self) -> None:
        # A modified file, a new file in a new folder, and a deletion.
        write(self.ios / "App.swift", "print(2)\n")
        write(self.ios / "New" / "Deep" / "New.swift", "new\n")
        (self.ios / "Info.plist").unlink()
        dirty = compute(self.subject, xcode=XCODE)
        self.assertTrue(dirty.dirty)
        commit(self.top)
        committed = compute(self.subject, xcode=XCODE)
        self.assertFalse(committed.dirty)
        self.assertEqual(committed.key, dirty.key)
        # The key is the folder's real tree id once committed.
        tree = git(self.top, "rev-parse", "HEAD:ios").strip()
        self.assertEqual(fingerprint._git_state(fingerprint._repository(self.ios)).tree, tree)

    def test_new_file_committed_or_not(self) -> None:
        write(self.ios / "New.swift", "new\n")
        untracked = self.key(self.subject)
        git(self.top, "add", "ios/New.swift")
        self.assertEqual(self.key(self.subject), untracked)
        commit(self.top)
        self.assertEqual(self.key(self.subject), untracked)

    def test_whole_repository_committed_or_not(self) -> None:
        whole = self.app(self.top)
        write(self.top / "backend" / "server.py", "edited\n")
        write(self.top / "z.txt", "z\n")
        write(self.top / "ios" / "App.swift", "print(2)\n")
        dirty = self.key(whole)
        commit(self.top)
        self.assertEqual(self.key(whole), dirty)

    def test_new_root_committed_or_not(self) -> None:
        new = write(self.top / "watch" / "Watch.swift", "watch\n").parent
        app = self.app(new)
        untracked = self.key(app)
        commit(self.top)
        self.assertEqual(self.key(app), untracked)

    def test_executable_bit_counts(self) -> None:
        script = write(self.ios / "build.sh", "#!/bin/sh\n")
        commit(self.top)
        plain = self.key(self.subject)
        script.chmod(0o755)
        executable = self.key(self.subject)
        self.assertNotEqual(executable, plain)
        self.assertTrue(compute(self.subject, xcode=XCODE).dirty)
        script.chmod(0o644)
        self.assertEqual(self.key(self.subject), plain)
        # Committed as executable: the same as the uncommitted chmod.
        script.chmod(0o755)
        commit(self.top)
        self.assertEqual(self.key(self.subject), executable)

    def test_executable_bit_ignored_when_git_ignores_it(self) -> None:
        script = write(self.ios / "build.sh", "#!/bin/sh\n")
        commit(self.top)
        plain = self.key(self.subject)
        git(self.top, "config", "core.fileMode", "false")
        script.chmod(0o755)
        self.assertEqual(self.key(self.subject), plain)
        write(script, "#!/bin/sh\necho\n")
        edited = self.key(self.subject)
        commit(self.top)
        self.assertEqual(self.key(self.subject), edited)

    def test_folder_with_a_newline_committed_or_not(self) -> None:
        odd = write(self.ios / "odd\nfolder" / "Inner.swift", "one\n")
        commit(self.top)
        write(odd, "two\n")
        dirty = self.key(self.subject)
        commit(self.top)
        self.assertEqual(self.key(self.subject), dirty)

    def test_file_replaced_by_folder(self) -> None:
        (self.ios / "Info.plist").unlink()
        write(self.ios / "Info.plist" / "Inner.txt", "inner\n")
        dirty = self.key(self.subject)
        self.assertNotEqual(dirty, self.original)
        commit(self.top)
        self.assertEqual(self.key(self.subject), dirty)

    def test_symlink(self) -> None:
        link = self.ios / "Link"
        os.symlink("App.swift", link)
        first = self.key(self.subject)
        self.assertNotEqual(first, self.original)
        link.unlink()
        os.symlink("Info.plist", link)
        self.assertNotEqual(self.key(self.subject), first)


class SourceTests(Case):
    def test_clean_dirty_and_detached(self) -> None:
        top = self.repository()
        app = self.app(top / "ios")
        short = git(top, "rev-parse", "--short", "HEAD").strip()
        self.assertEqual(compute(app, xcode=XCODE).source, f"main@{short}")
        write(top / "ios" / "App.swift", "edited\n")
        self.assertEqual(compute(app, xcode=XCODE).source, f"main@{short}+changes")
        git(top, "checkout", "-q", "--", "ios/App.swift")
        git(top, "checkout", "-q", "--detach")
        self.assertEqual(compute(app, xcode=XCODE).source, f"HEAD@{short}")

    def test_dirty_outside_the_root_is_clean(self) -> None:
        top = self.repository()
        write(top / "backend" / "server.py", "edited\n")
        self.assertNotIn("+changes", compute(self.app(top / "ios"), xcode=XCODE).source)

    def test_unborn_head(self) -> None:
        top = self.folder / "empty"
        top.mkdir()
        git(top, "init", "-q")
        app = self.app(top)
        empty = compute(app, xcode=XCODE)
        self.assertFalse(empty.dirty)
        self.assertEqual(empty.source, "main")
        write(top / "App.swift", "one\n")
        first = compute(app, xcode=XCODE)
        self.assertNotEqual(first.key, empty.key)
        self.assertEqual(first.source, "main+changes")
        git(top, "add", "App.swift")
        self.assertEqual(self.key(app), first.key)
        write(top / "App.swift", "two\n")
        self.assertNotEqual(self.key(app), first.key)
        self.assertEqual(recent_commits(app), [])

    def test_outside_git(self) -> None:
        plain = write(self.folder / "plain" / "App.swift", "one\n").parent
        result = compute(self.app(plain), xcode=XCODE)
        self.assertEqual(result.source, "")
        self.assertFalse(result.dirty)


class WalkTests(Case):
    def test_edit_changes_it(self) -> None:
        plain = write(self.folder / "plain" / "App.swift", "one\n").parent
        app = self.app(plain)
        first = self.key(app)
        self.assertEqual(self.key(app), first)
        write(plain / "App.swift", "two!\n")
        self.assertNotEqual(self.key(app), first)

    def test_same_size_edit_changes_it(self) -> None:
        path = write(self.folder / "plain" / "App.swift", "one\n")
        app = self.app(path.parent)
        first = self.key(app)
        path.write_text("two\n")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
        self.assertNotEqual(self.key(app), first)

    def test_skips_noise(self) -> None:
        plain = write(self.folder / "plain" / "App.swift", "one\n").parent
        (plain / "App.xcodeproj").mkdir()
        app = self.app(plain)
        first = self.key(app)
        for noise in ("build/out.o", ".build/x", "DerivedData/x", "App.xcodeproj/xcuserdata/me.plist", ".DS_Store",
                      ".git/HEAD"):
            write(plain / noise, "noise\n")
        self.assertEqual(self.key(app), first)

    def test_executable_bit_counts(self) -> None:
        path = write(self.folder / "plain" / "build.sh", "#!/bin/sh\n")
        app = self.app(path.parent)
        first = self.key(app)
        modified = path.stat().st_mtime_ns
        path.chmod(0o755)
        os.utime(path, ns=(path.stat().st_atime_ns, modified))
        self.assertNotEqual(self.key(app), first)
        path.chmod(0o644)
        os.utime(path, ns=(path.stat().st_atime_ns, modified))
        self.assertEqual(self.key(app), first)

    def test_executable_file_input(self) -> None:
        path = write(self.folder / "build.sh", "#!/bin/sh\n")
        app = self.app(write(self.folder / "plain" / "App.swift", "one\n").parent, inputs=[path])
        first = self.key(app)
        path.chmod(0o755)
        self.assertNotEqual(self.key(app), first)

    def test_new_and_deleted_files(self) -> None:
        plain = write(self.folder / "plain" / "App.swift", "one\n").parent
        app = self.app(plain)
        first = self.key(app)
        extra = write(plain / "Ünï code.swift", "x\n")
        self.assertNotEqual(self.key(app), first)
        extra.unlink()
        self.assertEqual(self.key(app), first)

    def test_missing_folder_raises(self) -> None:
        with self.assertRaises(FingerprintError) as raised:
            compute(self.app(self.folder / "gone"), xcode=XCODE)
        self.assertIn("gone", str(raised.exception))
        with self.assertRaises(FingerprintError):
            compute(App(id="app", name="App"), xcode=XCODE)


class InputTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.top = self.repository()

    def test_input_in_another_repository(self) -> None:
        other = self.repository("shared")
        app = self.app(self.top / "ios", inputs=[other / "ios"])
        first = compute(app, xcode=XCODE)
        self.assertNotEqual(first.key, self.key(self.app(self.top / "ios")))
        write(other / "ios" / "App.swift", "edited\n")
        changed = compute(app, xcode=XCODE)
        self.assertNotEqual(changed.key, first.key)
        self.assertTrue(changed.dirty)
        self.assertTrue(changed.source.endswith("+changes"))
        write(other / "backend" / "server.py", "edited\n")
        write(other / "ios" / "App.swift", "print(1)\n")
        self.assertEqual(self.key(app), first.key)

    def test_input_in_the_same_repository(self) -> None:
        app = self.app(self.top / "ios", inputs=[self.top / "backend"])
        first = self.key(app)
        write(self.top / "backend" / "server.py", "edited\n")
        self.assertNotEqual(self.key(app), first)

    def test_input_outside_git(self) -> None:
        plain = write(self.folder / "plain" / "Shared.swift", "one\n").parent
        app = self.app(self.top / "ios", inputs=[plain])
        first = self.key(app)
        write(plain / "Shared.swift", "two!\n")
        self.assertNotEqual(self.key(app), first)

    def test_file_input(self) -> None:
        shared = write(self.folder / "Shared.xcconfig", "A = 1\n")
        app = self.app(self.top / "ios", inputs=[shared])
        first = self.key(app)
        os.utime(shared, (0, 0))
        self.assertEqual(self.key(app), first)
        write(shared, "A = 2\n")
        self.assertNotEqual(self.key(app), first)
        write(shared, "A = 1\n")
        self.assertEqual(self.key(app), first)

    def test_missing_input_raises(self) -> None:
        with self.assertRaises(FingerprintError) as raised:
            compute(self.app(self.top / "ios", inputs=[self.folder / "nowhere"]), xcode=XCODE)
        self.assertIn("nowhere", str(raised.exception))


class BuilderTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.folder_app = write(self.folder / "plain" / "App.swift", "one\n").parent

    def test_builder_settings_count(self) -> None:
        base = self.app(self.folder_app)
        keys = {
            self.key(base),
            self.key(self.app(self.folder_app, command="make ipa")),
            self.key(self.app(self.folder_app, command="make ipa2")),
            self.key(self.app(self.folder_app, scheme="App")),
            self.key(self.app(self.folder_app, scheme="Other")),
            self.key(self.app(self.folder_app, configuration="Release")),
            self.key(self.app(self.folder_app, project=self.folder_app / "App.xcodeproj")),
        }
        self.assertEqual(len(keys), 7)

    def test_builder_version_counts(self) -> None:
        app = self.app(self.folder_app)
        first = self.key(app)
        with mock.patch.object(xcode_build, "BUILDER_VERSION", xcode_build.BUILDER_VERSION + 1):
            self.assertNotEqual(self.key(app), first)
        self.assertEqual(self.key(app), first)

    def test_xcode_counts(self) -> None:
        app = self.app(self.folder_app)
        self.assertNotEqual(self.key(app, xcode="Xcode 16.1 (16B40)"), self.key(app, xcode="Xcode 16.2 (16C5032a)"))
        with mock.patch.object(fingerprint, "xcode_identity", return_value="Xcode 9 (9A1)"):
            self.assertEqual(compute(app).key, self.key(app, xcode="Xcode 9 (9A1)"))


class XcodeTests(Case):
    def developer(self, version: str, build: str) -> Path:
        contents = self.folder / "Xcode.app" / "Contents"
        (contents / "Developer").mkdir(parents=True, exist_ok=True)
        with open(contents / "version.plist", "wb") as file:
            plistlib.dump({"CFBundleShortVersionString": version, "ProductBuildVersion": build}, file)
        return contents / "Developer"

    def test_reads_version_plist(self) -> None:
        developer = self.developer("16.2", "16C5032a")
        self.assertEqual(xcode_identity(str(developer)), "Xcode 16.2 (16C5032a)")
        plist = developer.parent / "version.plist"
        self.developer("16.3", "16E140")
        stat = plist.stat()
        os.utime(plist, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        self.assertEqual(xcode_identity(str(developer)), "Xcode 16.3 (16E140)")

    def test_developer_dir_environment(self) -> None:
        developer = self.developer("15.0", "15A240d")
        with mock.patch.dict(os.environ, {"DEVELOPER_DIR": str(developer)}):
            self.assertEqual(xcode_identity(), "Xcode 15.0 (15A240d)")
            app = self.app(self.folder / "Xcode.app")
            with_15 = compute(app).key
            self.developer("16.0", "16A242d")
            plist = developer.parent / "version.plist"
            os.utime(plist, ns=(plist.stat().st_atime_ns, plist.stat().st_mtime_ns + 1_000_000))
            self.assertNotEqual(compute(app).key, with_15)

    def test_command_line_tools_only(self) -> None:
        tools = self.folder / "CommandLineTools"
        tools.mkdir()
        self.assertEqual(xcode_identity(str(tools)), f"path {tools}")


class RecentCommitsTests(Case):
    def test_scoped_to_the_folder(self) -> None:
        top = self.repository()
        write(top / "ios" / "App.swift", "two\n")
        commit(top, "iOS: second")
        write(top / "backend" / "server.py", "two\n")
        commit(top, "Backend only")
        write(top / "ios" / "Ünï.swift", "x\n")
        commit(top, "iOS: thïrd")
        commits = recent_commits(self.app(top / "ios"))
        self.assertEqual([entry.subject for entry in commits], ["iOS: thïrd", "iOS: second", "initial"])
        head = git(top, "rev-parse", "--short", "HEAD").strip()
        self.assertEqual(commits[0].sha, head)
        self.assertRegex(commits[0].date, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d([+-]\d\d:\d\d|Z)$")
        self.assertEqual(len(recent_commits(self.app(top / "ios"), limit=2)), 2)
        self.assertEqual(len(recent_commits(self.app(top))), 4)

    def test_outside_git(self) -> None:
        plain = write(self.folder / "plain" / "App.swift", "one\n").parent
        self.assertEqual(recent_commits(self.app(plain)), [])
        self.assertEqual(recent_commits(self.app(self.folder / "gone")), [])
        self.assertEqual(recent_commits(App(id="app", name="App")), [])


if __name__ == "__main__":
    unittest.main()
