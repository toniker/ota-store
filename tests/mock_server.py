"""A stand-in for the store's server, for working on the page (`ota_store/web/`).

    python3 tests/mock_server.py [--port 8799] [--step 2]

It serves `web/` and the JSON API from docs/DESIGN.md with fake apps in every
state, and simulates builds: one worker, `==> ` steps every `--step` seconds,
queueing, cache hits, failures and logs. Nothing is real and nothing persists.

Install links: a simulator (or a desktop browser) can't install an .ipa, so
`installUrl` is `/mock/install/<id>/<key>`, which answers 204 No Content.
Navigating to it leaves the page where it is, as an itms-services link does,
and the terminal prints what would have been installed.

`POST /mock/touch/<id>` changes an app's sources, so it turns `changed`.
"""

from __future__ import annotations

import argparse
import json
import queue
import secrets
import struct
import sys
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit, parse_qs

WEB = Path(__file__).resolve().parent.parent / "ota_store" / "web"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
STEP = 2.0
LOCK = threading.RLock()
QUEUE: "queue.Queue[str]" = queue.Queue()


def iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).replace(microsecond=0).isoformat()


def icon_png(top, bottom, size=114) -> bytes:
    """A gradient icon with a light disc in the middle, so icons look like icons."""
    rows = []
    centre, radius = size / 2, size * 0.28
    for y in range(size):
        t = y / (size - 1)
        base = bytes(round(a + (b - a) * t) for a, b in zip(top, bottom))
        row = bytearray(b"\x00")
        for x in range(size):
            inside = (x - centre) ** 2 + (y - centre) ** 2 < radius ** 2
            row += bytes(min(255, c + 90) for c in base) if inside else base
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def build(fingerprint, version, number, size, age_hours, source, duration=38.4, expires_days=300.0,
          devices=3, configuration="Debug"):
    return {"key": secrets.token_hex(8), "fingerprint": fingerprint, "version": version, "build": str(number),
            "size": size, "builtAt": iso(-timedelta(hours=age_hours)), "duration": duration, "source": source,
            "configuration": configuration,
            "expiresAt": iso(timedelta(days=expires_days)) if expires_days is not None else None,
            "devices": devices if expires_days is not None else None}


def commits(*subjects):
    return [{"sha": secrets.token_hex(20), "subject": subject, "date": iso(-timedelta(hours=3 * i + 1))}
            for i, subject in enumerate(subjects)]


def xcode_log(name, targets, error=None):
    lines = ["==> Preparing", f"Command line invocation:\n    /Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild build "
             f"-project {name}.xcodeproj -scheme {name} -configuration Debug -destination generic/platform=iOS",
             "Resolve Package Graph", "Resolved source packages:", "  swift-collections: 1.1.4"]
    for target in targets:
        lines += [f"==> Building {target}",
                  f"SwiftCompile normal arm64 Compiling\\ {target}.swift (in target '{target}' from project '{name}')",
                  f"    cd /path/to/Developer/{name.lower()}/ios",
                  f"Ld /path/to/Library/Caches/ota-store/DerivedData/{name.lower()}/Build/Products/Debug-iphoneos/{target} "
                  f"normal (in target '{target}' from project '{name}')"]
        if error and target == targets[-1]:
            lines += [f"/path/to/Developer/{name.lower()}/ios/{target}/Store.swift:42:17: error: {error}",
                      "        let store = LedgerStore(context: context)", "                    ^~~~~~~~~~~",
                      "** BUILD FAILED **"]
            return lines
    return lines + ["==> Packaging", "** BUILD SUCCEEDED **"]


# The fake apps: one of each state the page has to show.
APPS = {}


def add(id, name, bundle_id=None, buildable=True, problem=None, colours=None, builds=(), current=None,
        targets=("App",), fail=None, info=None, history=(), log=None):
    fingerprint = current or secrets.token_hex(32)
    APPS[id] = {"id": id, "name": name, "bundleId": bundle_id, "buildable": buildable, "problem": problem,
                "icon": icon_png(*colours) if colours else None, "builds": list(builds), "fingerprint": fingerprint,
                "targets": list(targets), "fail": fail, "commits": commits(*history),
                "info": dict({"dir": f"~/Developer/{id}/ios", "project": f"{targets[-1]}.xcodeproj", "scheme": targets[-1],
                              "configuration": "Debug", "command": None}, **(info or {})) if buildable else
                        {"dir": None, "project": None, "scheme": None, "configuration": None, "command": None},
                "log": log, "job": None}


def seed():
    fp = secrets.token_hex(32)
    add("trailmap", "Trail Map", "com.example.TrailMap", colours=((96, 170, 255), (20, 70, 170)), current=fp,
        builds=[build(fp, "2.3", 41, 3_014_644, 2, "main@f2d6080+changes"),
                build(secrets.token_hex(32), "2.3", 40, 2_998_000, 30, "main@8a1c2e4")],
        targets=("TrailMapKit", "TrailMap"),
        history=("Show the route on the lock screen", "Fix distance rounding", "Bump to 2.3"),
        log="\n".join(xcode_log("TrailMap", ["TrailMapKit", "TrailMap"])))
    add("pocketjournal", "Pocket Journal", "com.example.pocketjournal", colours=((200, 120, 255), (90, 30, 150)),
        builds=[build(secrets.token_hex(32), "1.4", 12, 8_420_113, 5, "main@1b2c3d4"),
                build(secrets.token_hex(32), "1.4", 11, 8_401_000, 50, "main@9e8f7a6"),
                build(secrets.token_hex(32), "1.3", 9, 8_100_000, 24 * 40, "main@0a0b0c0", expires_days=-2)],
        targets=("PocketJournalCore", "PocketJournalUI", "PocketJournal"),
        history=("Add the morning digest", "Group notifications by thread", "Retry uploads on wake"),
        log="\n".join(xcode_log("PocketJournal", ["PocketJournalCore", "PocketJournalUI", "PocketJournal"])))
    add("weatherdeck", "Weather Deck", "com.example.WeatherDeck", colours=((255, 190, 80), (220, 80, 30)),
        builds=[build(secrets.token_hex(32), "1.0", 7, 5_202_000, 26, "develop@4d5e6f7")],
        targets=[f"Module{i}" for i in range(1, 25)] + ["WeatherDeck"],
        history=("Translate the onboarding", "Speed up the camera"),
        info={"command": "tuist generate --no-open && \"$OTA_STORE\" xcode-build"})
    add("atlas", "Atlas", None, builds=[], targets=("AtlasMaps", "Atlas"),
        history=("First commit",), info={"dir": "~/Developer/atlas"})
    add("ledger", "Ledger", "com.example.Ledger", colours=((120, 220, 120), (20, 120, 50)),
        builds=[build(secrets.token_hex(32), "0.9", 3, 1_750_000, 72, "main@abc1234")],
        targets=("LedgerData", "Ledger"), fail="Cannot find 'LedgerStore' in scope",
        history=("Move the store to SwiftData (WIP)",),
        log="\n".join(xcode_log("Ledger", ["LedgerData", "Ledger"], "Cannot find 'LedgerStore' in scope")))
    add("orbit", "Orbit", "com.example.Orbit", colours=((90, 90, 110), (20, 20, 35)),
        problem="Folder not found: ~/Developer/orbit/ios",
        builds=[build(secrets.token_hex(32), "3.1", 88, 12_300_000, 24 * 9, "main@77aa88b")])
    add("partner-sdk", "Partner SDK Demo", "com.partner.sdk.demo", buildable=False,
        colours=((255, 110, 110), (170, 20, 40)),
        builds=[build(secrets.token_hex(32), "5.0.1", 501, 21_400_000, 24 * 3, "", duration=None,
                      configuration=None)])
    add("sketchpad", "Sketchpad", None, buildable=False)
    add("field-survey-companion-enterprise-edition", "The Extraordinarily Long-Named Field Survey Companion",
        "com.example.enterprise.fieldsurvey.companion.internal",
        builds=[build(secrets.token_hex(32), "12.0.0-beta.3", 1234, 64_100_000, 24 * 6, "feature/offline-sync-with-a-very-long-branch-name@0f1e2d3+changes")],
        targets=("Survey",),
        info={"dir": "~/Developer/clients/acme/field-survey-companion/apps/ios/FieldSurvey",
              "command": "cd .. && make bootstrap && bundle exec fastlane build_for_ota output:\"$OTA_OUTPUT\""},
        history=("A commit subject long enough that it has to wrap over several lines on a phone held upright",
                 "<img src=x onerror=alert(1)> escapes, doesn't run"))
    # A build already running when the page opens, as if started from the CLI,
    # and a failure the store still remembers.
    start_job(APPS["weatherdeck"])
    ledger = APPS["ledger"]
    ledger["job"] = {"state": "failed", "step": "Building Ledger", "error": ledger["fail"],
                     "queued_at": time.time() - 60, "started_at": time.time() - 58, "finished_at": time.time() - 40,
                     "build": None, "cached": False}


def installable(b):
    return b["expiresAt"] is None or datetime.fromisoformat(b["expiresAt"]) > datetime.now(timezone.utc) + timedelta(hours=1)


def latest(app):
    good = [b for b in app["builds"] if installable(b)]
    return max(good, key=lambda b: b["builtAt"]) if good else None


def freshness(app):
    if not app["buildable"] or app["problem"]:
        return None
    if any(b["fingerprint"] == app["fingerprint"] and installable(b) for b in app["builds"]):
        return "current"
    return "changed" if app["builds"] else "unbuilt"


def build_json(app, b):
    return {"key": b["key"], "version": b["version"], "build": b["build"], "size": b["size"],
            "builtAt": b["builtAt"], "duration": b["duration"], "source": b["source"],
            "configuration": b["configuration"], "bundleId": app["bundleId"] or f"com.example.{app['id']}",
            "expiresAt": b["expiresAt"], "devices": b["devices"], "installable": installable(b),
            "installUrl": f"/mock/install/{quote(app['id'])}/{b['key']}",
            "iconUrl": f"/apps/{quote(app['id'])}/{b['key']}/icon.png" if app["icon"] else None}


def status_json(app):
    job = app["job"]
    if job is None:
        return {"state": "idle", "step": "", "elapsed": 0}
    now = time.time()
    if job["state"] == "queued":
        elapsed = now - job["queued_at"]
    elif job["state"] == "building":
        elapsed = now - job["started_at"]
    else:
        elapsed = (job["finished_at"] or now) - (job["started_at"] or job["queued_at"])
    status = {"state": job["state"], "step": job["step"], "elapsed": int(elapsed)}
    if job["state"] == "queued":
        running = next((a for a in APPS.values() if a["job"] and a["job"]["state"] == "building"), None)
        status["step"] = f"Waiting for {running['name']}" if running else "Waiting"
    if job["state"] == "failed":
        status["error"] = job["error"]
    if job["state"] == "done":
        status["build"] = build_json(app, job["build"])
        if job.get("cached"):
            status["cached"] = True
    return status


def app_json(app):
    b = latest(app)
    return {"id": app["id"], "name": b and b.get("name") or app["name"], "bundleId": app["bundleId"] if b else None,
            "buildable": app["buildable"], "freshness": freshness(app), "problem": app["problem"],
            "latest": build_json(app, b) if b else None, "status": status_json(app),
            "iconUrl": f"/apps/{quote(app['id'])}/icon.png" if app["icon"] and b else None}


def start_job(app):
    app["job"] = {"state": "queued", "step": "Waiting", "error": None, "queued_at": time.time(),
                  "started_at": None, "finished_at": None, "build": None, "cached": False}
    QUEUE.put(app["id"])


def request(app):
    """Builder.request: join a build in flight, else a cache hit, else a new build."""
    job = app["job"]
    if job and job["state"] in ("queued", "building"):
        return
    current = next((b for b in app["builds"] if b["fingerprint"] == app["fingerprint"] and installable(b)), None)
    if current:
        app["job"] = {"state": "done", "step": "", "error": None, "queued_at": time.time(), "started_at": time.time(),
                      "finished_at": time.time(), "build": current, "cached": True}
    else:
        start_job(app)


def worker():
    while True:
        app = APPS[QUEUE.get()]
        with LOCK:
            job = app["job"]
            job.update(state="building", started_at=time.time(), step="Preparing")
            app["log"] = ""
        steps = xcode_log(app["info"]["scheme"], app["targets"], app["fail"])
        if app["problem"]:
            steps = []
        for line in steps:
            with LOCK:
                app["log"] += line + "\n"
                if line.startswith("==> "):
                    job["step"] = line[4:]
            if line.startswith("==> "):
                time.sleep(STEP)
        with LOCK:
            job["finished_at"] = time.time()
            if app["problem"]:
                job.update(state="failed", error=app["problem"])
                app["log"] = app["problem"] + "\n"
            elif app["fail"]:
                job.update(state="failed", error=app["fail"])
            else:
                previous = latest(app) or {"version": "1.0", "build": "0", "size": 4_000_000}
                new = build(app["fingerprint"], previous["version"], int(previous["build"]) + 1,
                            previous["size"] + 1234, 0, "main@" + secrets.token_hex(4)[:7],
                            duration=round(job["finished_at"] - job["started_at"], 1))
                app["builds"] = [new] + app["builds"][:2]
                if not app["bundleId"]:
                    app["bundleId"] = f"com.example.{app['id']}"
                job.update(state="done", build=new)


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body=b"", kind="application/json", head=False):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_json(self, value, code=200):
        self.send(code, json.dumps(value).encode())

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        with LOCK:
            if url.path == "/":
                return self.send(200, (WEB / "index.html").read_bytes(), TYPES[".html"], head)
            if url.path == "/favicon.ico":
                return self.send(200, (WEB / "favicon-32.png").read_bytes(), "image/png", head)
            if len(parts) == 2 and parts[0] == "static" and parts[1] not in ("..", ".") and (WEB / parts[1]).is_file():
                path = WEB / parts[1]
                return self.send(200, path.read_bytes(), TYPES.get(path.suffix, "application/octet-stream"), head)
            if parts == ["api", "apps"]:
                order = sorted(APPS.values(), key=lambda a: (latest(a) or {}).get("builtAt", ""), reverse=True)
                return self.send_json({"title": "Apps", "apps": [app_json(a) for a in order]})
            app = APPS.get(parts[2]) if len(parts) >= 3 and parts[:2] == ["api", "apps"] else None
            if len(parts) >= 3 and parts[:2] == ["api", "apps"] and not app:
                return self.send_json({"error": f"No app {parts[2]}"}, 404)
            if app and len(parts) == 3:
                detail = app_json(app)
                detail.update(commits=app["commits"] if app["buildable"] else [], info=app["info"],
                              builds=[build_json(app, b)
                                      for b in sorted(app["builds"], key=lambda b: b["builtAt"], reverse=True)])
                return self.send_json(detail)
            if app and parts[3:] == ["status"]:
                return self.send_json(status_json(app))
            if app and parts[3:] == ["log"]:
                if app["log"] is None:
                    return self.send_json({"error": "No log"}, 404)
                return self.send(200, app["log"][-256 * 1024:].encode(), "text/plain; charset=utf-8", head)
            if len(parts) in (3, 4) and parts[0] == "apps" and parts[-1] == "icon.png":
                app = APPS.get(parts[1])
                if app and app["icon"]:
                    return self.send(200, app["icon"], "image/png", head)
            if len(parts) == 4 and parts[:2] == ["mock", "install"]:
                print(f"Would install {parts[2]} build {parts[3]}", flush=True)
                self.send_response(204)
                self.end_headers()
                return
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        with LOCK:
            app = APPS.get(parts[2]) if len(parts) == 4 and parts[:2] == ["api", "apps"] else None
            if app and parts[3] == "install":
                if not app["buildable"]:
                    b = latest(app)
                    if not b:
                        return self.send_json({"error": f"{app['name']} has nothing to install"}, 409)
                    return self.send_json({"state": "done", "step": "", "elapsed": 0,
                                           "build": build_json(app, b)})
                if parse_qs(url.query).get("force") == ["1"] and not (app["job"] and app["job"]["state"] in ("queued", "building")):
                    start_job(app)
                else:
                    request(app)
                return self.send_json(status_json(app))
            if len(parts) == 3 and parts[:2] == ["mock", "touch"] and parts[2] in APPS:
                APPS[parts[2]]["fingerprint"] = secrets.token_hex(32)
                return self.send_json({"ok": True})
        self.send_json({"error": "Not found"}, 404)

    def log_message(self, format, *args):
        if not self.path.startswith("/api/"):
            sys.stderr.write("%s\n" % (format % args))


def main():
    global STEP
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--step", type=float, default=2.0, help="seconds per build step")
    args = parser.parse_args()
    STEP = args.step
    seed()
    threading.Thread(target=worker, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Mock store on http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
