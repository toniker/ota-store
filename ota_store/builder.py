"""The build queue, and the decision a tap makes: install a stored build, or build one.

One worker thread builds one app at a time: builds share the keychain,
signing and the CPU. Each build runs the app's build command (see "The build
command contract" in docs/DESIGN.md) in its own process group, so a timeout
or a cancel stops everything it started, xcodebuild included.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import pwd
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from . import builds, config, fingerprint
from .builds import Build
from .config import App
from .fingerprint import Fingerprint

# The folder that contains the `ota_store` package, and the launcher in it.
REPOSITORY = Path(__file__).resolve().parent.parent
LAUNCHER = REPOSITORY / "ota-store"
# A build taking longer than this is stopped.
BUILD_TIMEOUT = 45 * 60
# How long a stopped build gets to exit after SIGTERM before it's killed.
KILL_GRACE = 5.0
# How much of an error message the page shows.
MAX_ERROR = 300

ACTIVE = ("queued", "building")


class Job:
    """One build of one app, from the tap that asked for it until it's done or failed."""

    def __init__(self, app: App):
        self.app = app
        self.state = "queued"  # then "building", and "done" or "failed"
        self.step = "Waiting"
        self.error: Optional[str] = None
        self.build: Optional[Build] = None
        self.queued_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        # The fingerprint the build was made from, computed when it started.
        self.fingerprint: Optional[Fingerprint] = None
        # Set to say why the build is being stopped: it's cancelled, or took too long.
        self._stop_reason: Optional[str] = None
        self._stop = threading.Event()
        # The build command's process group, while it runs.
        self._group: Optional[int] = None

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed")

    @property
    def elapsed(self) -> float:
        """Seconds queued, or building, or that the build took."""
        if self.started_at is None:
            return (self.finished_at or time.time()) - self.queued_at
        return (self.finished_at or time.time()) - self.started_at

    def __repr__(self) -> str:
        return f"<Job {self.app.id} {self.state} {self.step!r}>"


def command_for(app: App) -> List[str]:
    """The process a build of `app` runs: the built-in builder, or its command in the login shell."""
    if not app.command:
        return [sys.executable, "-m", "ota_store.xcode_build"]
    # LaunchAgents start with a bare PATH; a login shell gives the one a terminal has.
    shell = os.environ.get("OTA_STORE_SHELL")
    if not shell:
        try:
            shell = pwd.getpwuid(os.getuid()).pw_shell
        except KeyError:
            shell = ""
    return [shell or "/bin/sh", "-lc", app.command]


def environment_for(app: App, output: Path, derived_data: Path) -> Dict[str, str]:
    env = dict(os.environ)
    env.update({
        "OTA_OUTPUT": str(output),
        "OTA_DERIVED_DATA": str(derived_data),
        "OTA_APP_ID": app.id,
        "OTA_PROJECT": str(app.project) if app.project else "",
        "OTA_SCHEME": app.scheme or "",
        "OTA_CONFIGURATION": app.configuration,
        "OTA_STORE": str(LAUNCHER),
    })
    if not app.command:
        paths = [str(REPOSITORY)] + [path for path in env.get("PYTHONPATH", "").split(os.pathsep) if path]
        env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def error_message(errors: List[str], tail: List[str]) -> Optional[str]:
    """What went wrong, from a build's output: its first `error:` line, else its last line."""
    if errors:
        return errors[0][:MAX_ERROR]
    if tail:
        return tail[-1][:MAX_ERROR]
    return None


def _signal_group(pgid: int, number: int) -> None:
    try:
        os.killpg(pgid, number)
    except (ProcessLookupError, PermissionError):
        pass


def interrupt_on_termination() -> None:
    """Makes SIGTERM and SIGHUP raise KeyboardInterrupt, as Ctrl-C does, so the caller can `shutdown()`.

    launchd stops a LaunchAgent with SIGTERM (`service restart`, `bootout`), and
    closing a terminal sends SIGHUP. Call it from the main thread.
    """
    def interrupt(number, frame):
        raise KeyboardInterrupt
    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, interrupt)


def clean_up_work() -> None:
    """Deletes build folders left by a process that died mid-build.

    Only ones older than the build timeout: a build in another process (the
    CLI's, without a server) may be using a newer one.
    """
    work_root = config.cache() / "work"
    try:
        folders = list(work_root.iterdir())
    except OSError:
        return
    for folder in folders:
        try:
            if time.time() - folder.stat().st_mtime > BUILD_TIMEOUT + 60:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


class Builder:
    """The build queue. `request` is what a tap does; the page reads `job` to follow a build."""

    def __init__(self, start_worker: bool = True, timeout: float = BUILD_TIMEOUT,
                 kill_grace: float = KILL_GRACE):
        self.timeout = timeout
        self.kill_grace = kill_grace
        self._lock = threading.Lock()
        # Notified whenever a job's state or step changes.
        self._changed = threading.Condition(self._lock)
        self._jobs: Dict[str, Job] = {}
        self._queue: "queue.Queue[Job]" = queue.Queue()
        self._current: Optional[Job] = None
        # One per app: two taps on the same app decide one at a time, so they join one job.
        self._deciding: Dict[str, threading.Lock] = {}
        self._worker: Optional[threading.Thread] = None
        if start_worker:
            self._worker = threading.Thread(target=self._work, name="ota-store builder", daemon=True)
            self._worker.start()

    # MARK: - Asking for builds

    def request(self, app: App, force: bool = False) -> Union[Job, Build]:
        """What a tap on Install does: join the build in flight, or reuse a stored build, or build.

        Returns the Job to follow, or the stored Build to install at once.
        Without `force`, a stored installable build made from the app's
        current fingerprint is reused (and marked used).
        """
        with self._lock:
            deciding = self._deciding.setdefault(app.id, threading.Lock())
        with deciding:
            with self._lock:
                job = self._jobs.get(app.id)
                if job is not None and not job.finished:
                    return job
            if not force:
                try:
                    current = fingerprint.compute(app)
                except fingerprint.FingerprintError:
                    # The job fingerprints again when it starts, and fails with the reason.
                    current = None
                if current is not None:
                    stored = builds.find(app.id, current.key)
                    if stored is not None and stored.installable():
                        builds.mark_used(stored)
                        with self._lock:
                            # A finished build stays: another phone may still be following it
                            # to install it. A failure no longer says anything about the app.
                            finished = self._jobs.get(app.id)
                            if finished is not None and finished.state == "failed":
                                del self._jobs[app.id]
                                self._changed.notify_all()
                        return stored
            job = Job(app)
            with self._lock:
                self._jobs[app.id] = job
                self._changed.notify_all()
            self._queue.put(job)
            return job

    def job(self, app_id: str) -> Optional[Job]:
        """The app's job in flight, or its last finished one until the next request."""
        with self._lock:
            return self._jobs.get(app_id)

    def current(self) -> Optional[Job]:
        """The job being built."""
        with self._lock:
            return self._current

    def jobs(self) -> List[Job]:
        with self._lock:
            return list(self._jobs.values())

    def waiting_step(self, job: Job) -> str:
        """What a queued job shows: "Waiting for <the app being built>", or "Waiting"."""
        with self._lock:
            current = self._current
        if job.state == "queued" and current is not None and current is not job:
            return f"Waiting for {current.app.name}"
        return job.step if job.state != "queued" else "Waiting"

    def wait(self, job: Job, timeout: Optional[float] = None,
             on_step: Optional[Callable[[str], None]] = None) -> Job:
        """Waits until `job` is done or failed, or `timeout` passes, calling `on_step` with each new step."""
        deadline = None if timeout is None else time.monotonic() + timeout
        last_step = None
        while True:
            with self._lock:
                step, finished = job.step, job.finished
                if step == last_step and not finished:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return job
                    self._changed.wait(remaining)
                    step, finished = job.step, job.finished
            if on_step is not None and step != last_step:
                on_step(step)
            last_step = step
            if finished:
                return job

    def cancel(self, app_id: str) -> bool:
        """Stops the app's queued or running build. False if it has none."""
        with self._lock:
            job = self._jobs.get(app_id)
            if job is None or job.finished:
                return False
            if job.state == "queued":
                # The worker skips it when it comes up.
                self._finish(job, error="Cancelled")
                return True
            job._stop_reason = "Cancelled"
            group = job._group
        job._stop.set()
        # Signalled here too, not only by the worker: a CLI cancelling on Ctrl-C exits right after.
        if group is not None:
            _signal_group(group, signal.SIGTERM)
        return True

    # MARK: - Building

    def _finish(self, job: Job, error: Optional[str] = None, build: Optional[Build] = None) -> None:
        """Marks `job` done or failed. Call with the lock held."""
        job.state = "failed" if error is not None else "done"
        job.error = error
        job.build = build
        job.finished_at = time.time()
        if self._current is job:
            self._current = None
        self._changed.notify_all()

    def _set_step(self, job: Job, step: str) -> None:
        with self._lock:
            job.step = step
            self._changed.notify_all()

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            self.run_next(job)

    def run_next(self, job: Job) -> None:
        """Builds `job` on this thread, as the worker does. For the worker, and tests without one."""
        with self._lock:
            if job.state != "queued":
                return
            job.state = "building"
            job.step = "Preparing"
            job.started_at = time.time()
            self._current = job
            self._changed.notify_all()
        try:
            build = self._build(job)
        except BuildFailed as failure:
            error: Optional[str] = str(failure)
            build = None
        except Exception as exception:  # The store keeps running whatever a build does.
            error, build = f"{type(exception).__name__}: {exception}", None
        else:
            error = None
        with self._lock:
            self._finish(job, error=error, build=build)

    def _build(self, job: Job) -> Build:
        app = job.app
        if app.dir is None:
            raise BuildFailed(f"{app.id} has no dir, so it can't be built")
        try:
            job.fingerprint = fingerprint.compute(app)
        except fingerprint.FingerprintError as error:
            raise BuildFailed(str(error))

        if job._stop_reason is not None:
            raise BuildFailed(job._stop_reason)

        derived_data = config.derived_data(app.id)
        derived_data.mkdir(parents=True, exist_ok=True)
        work_root = config.cache() / "work"
        work_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(dir=work_root, prefix=f"{app.id}-"))
        try:
            output = work / "App.ipa"
            code, errors, tail = self._run(job, command_for(app), environment_for(app, output, derived_data))
            if job._stop_reason is not None:
                raise BuildFailed(job._stop_reason)
            if code != 0:
                raise BuildFailed(error_message(errors, tail) or f"The build command exited with {code}")
            if not output.is_file():
                # Its last line is likely to be a success message, so it isn't the reason.
                raise BuildFailed(error_message(errors, [])
                                  or "The build command finished without writing an .ipa to $OTA_OUTPUT")
            self._set_step(job, "Storing")
            duration = time.time() - (job.started_at or time.time())
            made_from = self._settled_fingerprint(job)
            try:
                return builds.store(app, output, made_from, duration)
            except builds.InvalidIPA as error:
                raise BuildFailed(str(error))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _settled_fingerprint(self, job: Job) -> Fingerprint:
        """What to store a finished build under: the fingerprint it started from, if the sources still match it.

        The build reads the sources while it runs, so if they changed meanwhile
        it may hold some of each. Then it's stored under a key no tap computes:
        it still installs as the latest build, but the next tap builds again.
        """
        started = job.fingerprint
        assert started is not None
        try:
            ended: Optional[str] = fingerprint.compute(job.app).key
        except fingerprint.FingerprintError:
            ended = None
        if ended == started.key:
            return started
        try:
            with open(config.log_path(job.app.id), "a", encoding="utf-8") as log:
                log.write("[ota-store] The sources changed during the build, so it won't be reused.\n")
        except OSError:
            pass
        mixed = hashlib.sha256(f"changed during the build\0{started.key}\0{ended}".encode()).hexdigest()
        return dataclasses.replace(started, key=mixed)

    def shutdown(self) -> None:
        """Stops the build in flight and everything it started, before this process exits.

        Builds run in their own process group, so nothing else would stop them,
        and an orphaned xcodebuild would keep writing to the DerivedData the
        next server's builds use.
        """
        with self._lock:
            job = self._current
        if job is None:
            return
        self.cancel(job.app.id)
        self.wait(job, timeout=self.kill_grace)
        with self._lock:
            group = job._group
        if group is not None:
            # The worker would do this after the grace period, but it dies with the process.
            _signal_group(group, signal.SIGKILL)

    def _run(self, job: Job, command: List[str], env: Dict[str, str]):
        """Runs the build command, logging its output and following its steps.

        Returns its exit code, the messages of its `error:` lines and its last lines.
        """
        app = job.app
        log_path = config.log_path(app.id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "w", encoding="utf-8", errors="replace")
        log.write(f"$ {app.command or ' '.join(command)}\n")
        log.flush()
        try:
            process = subprocess.Popen(
                command, cwd=str(app.dir), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", start_new_session=True,
            )
        except OSError as error:
            log.write(f"Can't run the build command: {error}\n")
            log.close()
            raise BuildFailed(f"Can't run the build command: {error}")

        errors: List[str] = []
        tail: List[str] = []
        output_lock = threading.Lock()
        # Set once the build is over: output from something that outlived it is
        # drained but not logged, or it would land in the next build's log.
        detached = threading.Event()

        def read() -> None:
            try:
                for line in process.stdout:
                    if detached.is_set():
                        continue
                    try:
                        log.write(line)
                        log.flush()
                    except OSError:
                        pass
                    line = line.strip()
                    if line.startswith("==> "):
                        self._set_step(job, line[4:].strip())
                    elif line:
                        with output_lock:
                            tail[:] = (tail + [line])[-20:]
                            if "error:" in line and len(errors) < 5:
                                # The message, without the file it's in.
                                errors.append(line.split("error:", 1)[1].strip())
            finally:
                process.stdout.close()
                log.close()

        reader = threading.Thread(target=read, name=f"ota-store log {app.id}", daemon=True)
        reader.start()

        # The group's id is the leader's pid, as it started a new session.
        group = process.pid
        with self._lock:
            job._group = group
        deadline = time.monotonic() + self.timeout
        stopping_since: Optional[float] = None
        while True:
            try:
                code = process.wait(timeout=0.05 if stopping_since is not None else 0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if stopping_since is None:
                timed_out = time.monotonic() > deadline
                if job._stop.is_set() or timed_out:
                    if job._stop_reason is None:
                        job._stop_reason = f"The build took longer than {_duration(self.timeout)}, so it was stopped"
                    _signal_group(group, signal.SIGTERM)
                    stopping_since = time.monotonic()
            elif time.monotonic() - stopping_since > self.kill_grace:
                _signal_group(group, signal.SIGKILL)
        # Whatever the command left running in its group goes with it.
        with self._lock:
            job._group = None
        _signal_group(group, signal.SIGKILL)
        # Something that left the group could still hold the output open; don't wait on it for long.
        reader.join(timeout=5)
        detached.set()
        with output_lock:
            errors_seen, tail_seen = list(errors), list(tail)
        if not reader.is_alive():
            with open(log_path, "a", encoding="utf-8") as footer:
                footer.write(f"\n[ota-store] exit {code} after {time.time() - (job.started_at or time.time()):.1f}s\n")
        return code, errors_seen, tail_seen


class BuildFailed(Exception):
    """A build didn't make an .ipa; the message is what the page shows."""


def _duration(seconds: float) -> str:
    if seconds >= 120:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds:g} seconds"
