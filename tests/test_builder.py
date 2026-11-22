import os
import pwd
import shlex
import sys
import threading
import time
from pathlib import Path
from unittest import TestCase, mock

from ota_store import builder, builds, config, fingerprint
from ota_store.builder import Builder, Job
from ota_store.builds import Build
from ota_store.config import App
from tests.helpers import fake_build_command, temp_store

WAIT = 15


def wait_until(condition, timeout: float = WAIT, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError("timed out waiting")


def process_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class BuilderTestCase(TestCase):
    """A temporary store, /bin/sh for build commands, and a counter of fake builds."""

    def setUp(self):
        store = temp_store()
        self.root = store.__enter__()
        self.addCleanup(store.__exit__, None, None, None)
        patcher = mock.patch.dict(os.environ, {"OTA_STORE_SHELL": "/bin/sh"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.counter = self.root / "counter"

    def make_app(self, app_id: str = "fake", name: str = "", command: str = None, **fake: str) -> App:
        folder = self.root / "src" / app_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "main.swift").write_text("print(1)\n")
        if command is None:
            command = fake_build_command(counter=self.counter, **fake)
        return App(id=app_id, name=name or app_id.title(), dir=folder, scheme="Fake", command=command)

    def builds_run(self) -> int:
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    def make_builder(self, **options) -> Builder:
        return Builder(**options)

    def finish(self, builder_: Builder, result) -> Job:
        self.assertIsInstance(result, Job)
        job = builder_.wait(result, timeout=WAIT)
        self.assertTrue(job.finished, job)
        return job


class CommandTests(BuilderTestCase):
    def test_built_in_builder_runs_directly_with_the_package_on_the_path(self):
        app = App(id="x", name="X", dir=self.root)
        self.assertEqual(builder.command_for(app), [sys.executable, "-m", "ota_store.xcode_build"])
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/elsewhere"}):
            env = builder.environment_for(app, self.root / "out" / "App.ipa", self.root / "dd")
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep), [str(builder.REPOSITORY), "/elsewhere"])
        self.assertTrue((builder.REPOSITORY / "ota_store" / "__init__.py").is_file())

    def test_custom_command_runs_in_the_login_shell(self):
        app = App(id="x", name="X", dir=self.root, command="make ipa")
        self.assertEqual(builder.command_for(app), ["/bin/sh", "-lc", "make ipa"])
        del os.environ["OTA_STORE_SHELL"]
        self.assertEqual(builder.command_for(app), [pwd.getpwuid(os.getuid()).pw_shell, "-lc", "make ipa"])
        env = builder.environment_for(app, self.root / "App.ipa", self.root / "dd")
        self.assertNotIn(str(builder.REPOSITORY), env.get("PYTHONPATH", ""))

    def test_command_gets_the_contract_environment_in_the_app_folder(self):
        app = self.make_app()
        app.project = app.dir / "Fake.xcodeproj"
        app.configuration = "Release"
        dump = self.root / "env.txt"
        app.command = f"env > {shlex.quote(str(dump))}; pwd >> {shlex.quote(str(dump))}; {app.command}"
        b = self.make_builder()
        job = self.finish(b, b.request(app))
        self.assertEqual(job.state, "done", job.error)
        lines = dump.read_text().splitlines()
        env = dict(line.split("=", 1) for line in lines if "=" in line)
        self.assertEqual(lines[-1], str(app.dir))
        self.assertEqual(env["OTA_APP_ID"], "fake")
        self.assertEqual(env["OTA_PROJECT"], str(app.dir / "Fake.xcodeproj"))
        self.assertEqual(env["OTA_SCHEME"], "Fake")
        self.assertEqual(env["OTA_CONFIGURATION"], "Release")
        self.assertEqual(env["OTA_STORE"], str(builder.REPOSITORY / "ota-store"))
        self.assertEqual(env["OTA_DERIVED_DATA"], str(config.derived_data("fake")))
        self.assertTrue(config.derived_data("fake").is_dir())
        self.assertEqual(Path(env["OTA_OUTPUT"]).name, "App.ipa")
        self.assertFalse(Path(env["OTA_OUTPUT"]).parent.exists(), "the work folder is cleaned up")

    def test_error_message(self):
        self.assertEqual(builder.error_message(["first", "second"], ["last"]), "first")
        self.assertEqual(builder.error_message([], ["a", "b"]), "b")
        self.assertIsNone(builder.error_message([], []))
        self.assertEqual(len(builder.error_message(["x" * 400], [])), 300)


class RequestTests(BuilderTestCase):
    def test_build_then_cache_hit(self):
        app = self.make_app()
        b = self.make_builder()
        job = self.finish(b, b.request(app))
        self.assertEqual(job.state, "done", job.error)
        self.assertIsInstance(job.build, Build)
        self.assertEqual(job.build.fingerprint, job.fingerprint.key)
        self.assertEqual(job.build.app_id, "fake")
        self.assertIsNotNone(job.started_at)
        self.assertGreaterEqual(job.finished_at, job.started_at)
        self.assertIs(b.job("fake"), job, "a finished job stays until the next request")

        log = config.log_path("fake").read_text()
        self.assertIn("==> Building Fake", log)
        self.assertIn("$ " + app.command, log)

        hit = b.request(app)
        self.assertIsInstance(hit, Build)
        self.assertEqual(hit.key, job.build.key)
        self.assertGreater(builds.get("fake", hit.key).last_used_at, job.build.last_used_at)
        self.assertIs(b.job("fake"), job, "another phone may still be following the finished build")
        self.assertEqual(self.builds_run(), 1)

    def test_a_cache_hit_clears_a_failure(self):
        flag = self.root / "fail"  # outside the app's folder, so it isn't in the fingerprint
        app = self.make_app(command=f"test -f {shlex.quote(str(flag))} && exit 3; "
                                    + fake_build_command(counter=self.counter))
        b = self.make_builder()
        self.assertEqual(self.finish(b, b.request(app)).state, "done")
        flag.touch()
        self.assertEqual(self.finish(b, b.request(app, force=True)).state, "failed")
        flag.unlink()
        self.assertIsInstance(b.request(app), Build)
        self.assertIsNone(b.job("fake"), "the failure no longer describes the app")

    def test_sources_changed_during_the_build_arent_reused(self):
        app = self.make_app()
        app.command = "echo changed > main.swift; " + app.command
        b = self.make_builder()
        job = self.finish(b, b.request(app))
        self.assertEqual(job.state, "done", job.error)
        self.assertNotEqual(job.build.fingerprint, job.fingerprint.key)
        self.assertIsNone(builds.find("fake", fingerprint.compute(app).key))
        self.assertIn("sources changed during the build", config.log_path("fake").read_text())
        self.assertTrue(job.build.installable(), "it's still the latest build to install")
        self.assertIsInstance(b.request(app), Job, "the next tap builds again")

    def test_a_change_builds_again_and_force_always_builds(self):
        app = self.make_app()
        b = self.make_builder()
        first = self.finish(b, b.request(app))
        (app.dir / "main.swift").write_text("print(2)\n")
        second = self.finish(b, b.request(app))
        self.assertNotEqual(first.build.key, second.build.key)
        self.assertEqual(self.builds_run(), 2)

        forced = self.finish(b, b.request(app, force=True))
        self.assertEqual(forced.state, "done")
        self.assertEqual(forced.build.key, second.build.key, "same sources, same key: replaced")
        self.assertEqual(self.builds_run(), 3)
        self.assertEqual(len(builds.list_builds("fake")), 2)

    def test_expired_profile_builds_again(self):
        app = self.make_app(expired="1")
        b = self.make_builder()
        job = self.finish(b, b.request(app))
        self.assertEqual(job.state, "done")
        self.assertFalse(job.build.installable())
        self.assertIsInstance(b.request(app), Job)
        self.finish(b, b.job("fake"))
        self.assertEqual(self.builds_run(), 2)

    def test_prunes_to_keep(self):
        app = self.make_app()
        app.keep = 2
        b = self.make_builder()
        for n in range(3):
            (app.dir / "main.swift").write_text(f"print({n})\n" * (n + 1))
            self.finish(b, b.request(app))
        self.assertEqual(len(builds.list_builds("fake")), 2)

    def test_concurrent_taps_join_one_job(self):
        app = self.make_app(sleep="0.3")
        b = self.make_builder()
        barrier = threading.Barrier(4)
        results = []

        def tap():
            barrier.wait()
            results.append(b.request(app))

        threads = [threading.Thread(target=tap) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({id(result) for result in results}), 1)
        self.finish(b, results[0])
        self.assertEqual(self.builds_run(), 1)

    def test_job_fingerprints_again_when_it_starts(self):
        app = self.make_app()
        b = self.make_builder(start_worker=False)
        before = fingerprint.compute(app)
        job = b.request(app)
        self.assertEqual(job.state, "queued")
        (app.dir / "main.swift").write_text("edited while it waited\n")
        b.run_next(job)
        self.assertEqual(job.state, "done", job.error)
        self.assertNotEqual(job.fingerprint.key, before.key)
        self.assertEqual(job.fingerprint, fingerprint.compute(app))
        self.assertIsInstance(b.request(app), Build)

    def test_wait_reports_steps(self):
        app = self.make_app(sleep="0.3")
        b = self.make_builder()
        steps = []
        job = b.wait(b.request(app), timeout=WAIT, on_step=steps.append)
        self.assertEqual(job.state, "done")
        self.assertIn("Building Fake", steps)
        self.assertEqual(steps[-1], job.step)
        self.assertTrue(all(a != b_ for a, b_ in zip(steps, steps[1:])), steps)

    def test_wait_times_out(self):
        app = self.make_app(sleep="1")
        b = self.make_builder()
        job = b.request(app)
        started = time.monotonic()
        self.assertFalse(b.wait(job, timeout=0.2).finished)
        self.assertLess(time.monotonic() - started, 0.9)
        b.cancel("fake")


class FailureTests(BuilderTestCase):
    def build(self, app: App, **options) -> Job:
        b = self.make_builder(**options)
        job = self.finish(b, b.request(app))
        self.assertEqual(job.state, "failed")
        self.assertIsNone(job.build)
        self.assertEqual(builds.list_builds(app.id), [])
        return job

    def test_error_line_without_its_file(self):
        self.assertEqual(self.build(self.make_app(fail="1")).error, "cannot find 'Foo' in scope")
        self.assertIn("** BUILD FAILED **", config.log_path("fake").read_text())

    def test_no_ipa_written(self):
        self.assertIn("without writing an .ipa", self.build(self.make_app(no_output="1")).error)

    def test_last_line_or_exit_code(self):
        self.assertEqual(self.build(self.make_app(command="echo one; echo 'it broke'; exit 2")).error, "it broke")
        self.assertEqual(self.build(self.make_app(command="exit 3")).error, "The build command exited with 3")
        long = self.build(self.make_app(command="echo error: " + "x" * 400 + "; exit 1")).error
        self.assertEqual(long, "x" * 300)

    def test_not_an_ipa(self):
        app = self.make_app(command='echo junk > "$OTA_OUTPUT"')
        self.assertIn("isn't an .ipa", self.build(app).error)

    def test_fingerprint_failure(self):
        app = self.make_app()
        app.dir = self.root / "gone"
        self.assertIn("folder not found", self.build(app).error)


class QueueTests(BuilderTestCase):
    # A command whose child ignores SIGTERM, so stopping it takes the whole group and a SIGKILL.
    def stubborn_app(self, app_id: str = "slow") -> App:
        pid_file = self.root / f"{app_id}.pid"
        command = (f"(trap '' TERM; exec /bin/sleep 30) & echo $! > {shlex.quote(str(pid_file))}; "
                   "echo '==> Sleeping'; wait")
        app = self.make_app(app_id, command=command)
        app.extra["pid_file"] = pid_file
        return app

    def grandchild(self, app: App) -> int:
        pid_file = app.extra["pid_file"]
        return int(wait_until(lambda: pid_file.exists() and pid_file.read_text().strip()))

    def test_waiting_for_the_app_being_built(self):
        slow = self.stubborn_app()
        slow.name = "Slow App"
        other = self.make_app("other")
        b = self.make_builder(kill_grace=0.2)
        slow_job = b.request(slow)
        wait_until(lambda: slow_job.state == "building")
        other_job = b.request(other)
        self.assertEqual(other_job.state, "queued")
        self.assertEqual(b.waiting_step(other_job), "Waiting for Slow App")
        self.assertIs(b.current(), slow_job)
        self.assertTrue(b.cancel("slow"))
        self.finish(b, other_job)
        self.assertEqual(other_job.state, "done")
        self.assertEqual(b.waiting_step(other_job), other_job.step)

    def test_timeout_kills_the_process_group(self):
        app = self.stubborn_app()
        b = self.make_builder(timeout=0.5, kill_grace=0.2)
        job = b.request(app)
        pid = self.grandchild(app)
        self.finish(b, job)
        self.assertEqual(job.state, "failed")
        self.assertIn("took longer than 0.5 seconds", job.error)
        wait_until(lambda: process_gone(pid), timeout=3)

    def test_cancel_kills_the_process_group(self):
        app = self.stubborn_app()
        b = self.make_builder(kill_grace=0.2)
        job = b.request(app)
        pid = self.grandchild(app)
        wait_until(lambda: job.step == "Sleeping")
        self.assertTrue(b.cancel("slow"))
        self.finish(b, job)
        self.assertEqual((job.state, job.error), ("failed", "Cancelled"))
        wait_until(lambda: process_gone(pid), timeout=3)
        self.assertFalse(b.cancel("slow"), "nothing left to cancel")

    def test_shutdown_kills_the_build_in_flight(self):
        app = self.stubborn_app()
        b = self.make_builder(kill_grace=0.2)
        job = b.request(app)
        pid = self.grandchild(app)
        wait_until(lambda: job.step == "Sleeping")
        b.shutdown()
        wait_until(lambda: process_gone(pid), timeout=3)
        b.shutdown()  # nothing in flight: a no-op

    def test_clean_up_work_deletes_only_old_folders(self):
        work = config.cache() / "work"
        old, new = work / "app-old", work / "app-new"
        old.mkdir(parents=True)
        new.mkdir()
        stale = time.time() - builder.BUILD_TIMEOUT - 120
        os.utime(old, (stale, stale))
        builder.clean_up_work()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_cancel_a_queued_job(self):
        app = self.make_app()
        b = self.make_builder(start_worker=False)
        job = b.request(app)
        self.assertTrue(b.cancel("fake"))
        self.assertEqual((job.state, job.error), ("failed", "Cancelled"))
        b.run_next(job)
        self.assertEqual(job.state, "failed")
        self.assertEqual(self.builds_run(), 0)
        self.assertFalse(b.cancel("nothing"))

    def test_leftover_processes_are_killed_after_a_success(self):
        pid_file = self.root / "bg.pid"
        app = self.make_app(command=f"/bin/sleep 30 > /dev/null 2>&1 & echo $! > {shlex.quote(str(pid_file))}; "
                                    + fake_build_command())
        b = self.make_builder()
        job = self.finish(b, b.request(app))
        self.assertEqual(job.state, "done", job.error)
        wait_until(lambda: process_gone(int(pid_file.read_text())), timeout=3)
