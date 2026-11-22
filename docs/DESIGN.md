# ota-store design

ota-store is a private app store that runs on your Mac. You register the iOS apps you work on;
the store lists them on a web page that phones open over HTTPS on a private network (Tailscale
by default). Tapping INSTALL builds the app from its working copy as it is now, shows a loader
while it builds, then prompts to install it. If nothing changed since the last build, the
store skips the build and prompts at once.

It's for developers who work through agents on their Mac, often away from it, and want the
agent's latest changes on their phone in seconds instead of a TestFlight round trip.

## Decisions

- **Python 3.9+, standard library only.** Every Mac with Xcode has `/usr/bin/python3` (3.9.6),
  so people install nothing else. No third-party packages, no build step. Code must run on
  3.9: `from __future__ import annotations` in every module, no `match`, no `X | Y` outside
  annotations, no `zip(strict=)`, `datetime.fromisoformat` only on what `isoformat()` wrote.
  Run the tests with `/usr/bin/python3`.
- **Decide at tap time, no file watcher.** On tap the store fingerprints the app's sources and
  reuses a stored build with the same fingerprint. Nothing runs between taps.
- **Debug builds into a warm, dedicated DerivedData per app** (`~/Library/Caches/ota-store/DerivedData/<id>`).
  Agents build for the simulator, which a device build can't reuse; Xcode's shared DerivedData
  also lets the store's build and the agent's lock each other out. A store-owned one stays warm
  between taps and has no contention. `xcodebuild build` + zipping the `.app` replaces
  archive + export.
- **The build command is the extension point.** An app has either the built-in Xcode builder
  or its own shell command; either way, the command writes an `.ipa` to `$OTA_OUTPUT`. The
  store owns everything after: reading the `.ipa`, its icon, storing it.
- **No auth.** The network is the boundary. The server listens on 127.0.0.1 only; something
  in front of it (`tailscale serve`, Caddy, nginx) terminates HTTPS with a certificate iOS trusts.
- **JSON API + a static page.** The page renders from `/api/apps`; the CLI and agents can use the
  same API. The page has the look of the iOS 5 App Store.
- **State outside the repository**, in `~/Library/Application Support/ota-store`, so the code can
  live anywhere (a clone, a Homebrew cellar).

## Layout

```
ota-store                   the executable launcher
ota_store/
  __init__.py               the version
  __main__.py               `python3 -m ota_store` → cli.main
  config.py                 locations, App, Config, load/save
  fingerprint.py            source fingerprints, recent commits
  xcode_build.py            the built-in Xcode build command
  icons.py                  finding an app's icon as a PNG
  builds.py                 stored builds: inspecting, storing, finding, pruning
  builder.py                the build queue and the cache decision
  server.py                 HTTP API, manifests, files, the page
  cli.py                    the `ota-store` commands
  service.py                LaunchAgent install/uninstall/start/stop
  doctor.py                 `ota-store doctor` checks
  web/                      index.html, app.css, app.js, icons
tests/
  helpers.py, fake_build.py temp_store(), make_ipa(), png(), fake_build_command()
  mock_server.py            the page against fake data, for working on web/
  test_<module>.py          one per module
docs/DESIGN.md              this file
README.md                   user-facing docs
```

## Locations

From `config.py`; `OTA_STORE_HOME` and `OTA_STORE_CACHE` move them (tests use `temp_store()`):

```
home()/config.json                 settings and the registered apps
home()/builds/<app id>/<key>/      one build: App.ipa, meta.json, icon.png (optional)
home()/logs/<app id>.log           the output of the app's last build
home()/logs/server.log             the server's log (written by launchd)
cache()/DerivedData/<app id>/      the app's warm DerivedData
```

## Config

`config.json`:

```json
{
  "title": "Apps",
  "url": "https://apps.example.ts.net",
  "apps": [
    {
      "id": "trailmap",
      "name": "TrailMap",
      "dir": "~/Developer/trailmap/ios",
      "project": "TrailMap.xcodeproj",
      "scheme": "TrailMap",
      "configuration": "Debug",
      "command": "tuist generate --no-open && \"$OTA_STORE\" xcode-build",
      "inputs": ["../shared"],
      "icon": "TrailMap/Assets.xcassets/AppIcon.appiconset",
      "keep": 3
    }
  ]
}
```

Everything but `id` is optional. `id` is a slug (`[a-z0-9][a-z0-9-]*`) used in URLs and paths;
it's not the bundle id, which comes from each built `.ipa`. `dir` makes an app buildable; an app
without one only installs builds published with `ota-store publish`. `project`, `icon` and
`inputs` are relative to `dir`. `title`, `host`, `port` and `url` can also come from
`OTA_STORE_TITLE`, `OTA_STORE_HOST`, `OTA_STORE_PORT` and `OTA_STORE_URL`.

`config.load_config()` returns a `Config` with `apps: Dict[str, App]` whose paths are absolute.
The server reads it on each request, so edits apply without a restart. Writers load with
`apply_environment=False`, change it, and `save_config()` it.

## The build command contract

A build runs one command in `app.dir`:

- With no `command`: the built-in builder, `[sys.executable, "-m", "ota_store.xcode_build"]`,
  run directly, with `PYTHONPATH` including the directory that contains `ota_store`.
- With a `command`: `[login shell, "-lc", command]`, the login shell from
  `pwd.getpwuid(os.getuid()).pw_shell`, so Homebrew, rbenv and the like are on `PATH` as in a
  terminal. LaunchAgents start with a bare environment otherwise.

Its environment is the store's, plus:

| Variable | Value |
|---|---|
| `OTA_OUTPUT` | absolute path the command must write the `.ipa` to (its folder exists; the file doesn't) |
| `OTA_DERIVED_DATA` | the app's warm DerivedData folder (exists) |
| `OTA_APP_ID` | the app's id |
| `OTA_PROJECT` | absolute path of `project`, or empty |
| `OTA_SCHEME` | `scheme`, or empty |
| `OTA_CONFIGURATION` | `configuration` (default `Debug`) |
| `OTA_STORE` | absolute path of the `ota-store` launcher, so a command can end with `"$OTA_STORE" xcode-build` |

stdout and stderr go to `logs/<id>.log`. A line starting with `==> ` names the current step,
which the page shows. The build succeeds if the command exits 0 and `$OTA_OUTPUT` is a file.
It runs in its own process group; a timeout (45 minutes) or cancel kills the whole group.

### The built-in Xcode builder (`xcode_build.py`)

`python3 -m ota_store.xcode_build` and `ota-store xcode-build` both run `xcode_build.main()`,
which reads the environment above:

1. Project: `OTA_PROJECT`, or the one `.xcworkspace` (not inside a `.xcodeproj`), or else the one
   `.xcodeproj`, in the current folder. Scheme: `OTA_SCHEME`, or the only scheme, or the one named
   like the project (`xcodebuild -list -json`). Say what's ambiguous if it can't decide.
2. `xcodebuild build -workspace|-project … -scheme … -configuration $OTA_CONFIGURATION
   -destination generic/platform=iOS -derivedDataPath $OTA_DERIVED_DATA -allowProvisioningUpdates
   -skipMacroValidation -skipPackagePluginValidation -hideShellScriptEnvironment
   COMPILER_INDEX_STORE_ENABLE=NO`. Signing is the project's own: automatic signing with the
   development team gives a development-signed app with a development profile.
3. Progress: print `==> ` steps (`Preparing`, `Building <target>` as targets change, taken from
   `(in target 'X' from project 'Y')`, then `Packaging`). Pass the rest of the output through.
4. The `.app`: in `$OTA_DERIVED_DATA/Build/Products/<configuration>-iphoneos/`; if there's more than
   one, the one named after the scheme, else ask `-showBuildSettings -json` for the app target's
   `FULL_PRODUCT_NAME`.
5. Package: clone it into `Payload/` in a temporary folder (`cp -Rc`, which is instant on APFS) and
   zip it to `$OTA_OUTPUT` (`/usr/bin/zip -qry -1`: keeps symlinks, compresses fast).

## Fingerprints (`fingerprint.py`)

```python
compute(app) -> Fingerprint(key: str, source: str, dirty: bool)   # raises FingerprintError
recent_commits(app, limit=5) -> List[Commit(sha, subject, date)]   # [] outside git
```

`key` is a sha256 over:

- the builder: the `command` (or "built-in" plus `xcode_build.BUILDER_VERSION`), `project`,
  `scheme`, `configuration`;
- the Xcode in use: version and build of the selected Xcode (`DEVELOPER_DIR` or `xcode-select -p`,
  then `../version.plist`), read cheaply and cached per path and modification time;
- each root, `app.dir` then `app.inputs`. In a git work tree: the tree id the root would have if
  everything on disk were committed. That's `HEAD:<path from the top>` when the root is clean, so
  commits outside the root don't count. Otherwise the paths `git status --porcelain -z
  --untracked-files=all` reports are laid over HEAD's tree, with their content ids and git modes
  as on disk, and only the folders on the way to a change are rebuilt. So the same files give the
  same key whether they're committed or not, and a `chmod +x` counts. `.gitignore` does the
  filtering. Submodules count by their checkout's tree. Outside git: a walk of paths, sizes,
  modification times and executable bits that skips `.git`, `xcuserdata`, `DerivedData`, `build`,
  `.build` and `.DS_Store`. A root that's a file is hashed by content.

It has to be fast (well under a second for a normal repository): the page computes it for every
app it lists. Run git as `git --no-optional-locks` with `GIT_TERMINAL_PROMPT=0` and a timeout, so it
never writes the index or waits for input: the store runs next to agents using the same repository. `source` is `branch@shortsha`, plus `+changes` when dirty; `""` outside git.

## Stored builds (`builds.py`)

`builds/<app id>/<key>/` holds `App.ipa`, `meta.json` and maybe `icon.png`, where `key` is the first
16 hex digits of the fingerprint, or of the `.ipa`'s sha256 for published ones. A build is written
to a temporary folder in `builds/<app id>/` and renamed into place, so a reader sees all of it or
none. Storing a build prunes the app's builds to its `keep` newest by `lastUsedAt`.

`meta.json`:

```json
{
  "key": "a1b2c3d4e5f60718",
  "fingerprint": "a1b2…full sha256",
  "appId": "trailmap",
  "name": "TrailMap",
  "bundleId": "com.example.TrailMap",
  "version": "1.0",
  "build": "1",
  "size": 3014644,
  "builtAt": "2026-11-21T10:02:03+00:00",
  "lastUsedAt": "2026-11-21T10:02:03+00:00",
  "duration": 41.2,
  "source": "main@f2d6080+changes",
  "configuration": "Debug",
  "profile": {"name": "iOS Team Provisioning Profile: *", "team": "Example Team",
              "expiresAt": "2027-11-21T10:00:00+00:00", "devices": 3},
  "hasIcon": true,
  "published": false
}
```

`name` is `CFBundleDisplayName`, else `CFBundleName`. `profile` is null without an
`embedded.mobileprovision`; it's read by cutting the XML plist out of the CMS envelope (from
`<?xml` to `</plist>`), with no tools. A profile with `ProvisionsAllDevices` has `devices: null`.

A stored build is **installable** if its `App.ipa` exists and its profile, if any, doesn't expire
in the next hour.

## The build queue and the cache decision (`builder.py`)

One worker thread builds one app at a time: builds share the keychain, signing and the CPU.

`Builder.request(app, force=False)` is what a tap does:

1. If the app has a queued or running job, return that job. Every tap joins the build in flight.
2. Unless `force`, compute the fingerprint; if a stored build has that fingerprint and is
   installable, mark it used and return it: a cache hit, no build.
3. Otherwise queue a new job and return it.

A job computes the fingerprint again when it starts, so edits made while it waited are in the
build, and once more when the command has finished. If the two differ, the sources changed
while Xcode was reading them, and the build may hold some of each: it's stored under a key no
tap computes. It still installs as the latest build, but the next tap builds again. A
failed fingerprint fails the job with its message. Job states: `queued`, `building`, `done`,
`failed`. The step comes from `==> ` lines; a failure's message is the first `error:` line (without
the file it's in), else the last line of output, cut to 300 characters. A job remembers the build
it produced. A finished job stays until a new job replaces it, so a phone still following it
can install it. A cache hit only clears a failed one.

Builds run in their own process group, so the server stops the one in flight when it gets
SIGTERM or SIGHUP (launchd's restart and unload) or Ctrl-C, instead of leaving xcodebuild
writing to the DerivedData the next server will use.

## Python interfaces between modules

What other modules (the CLI especially) may call. The owner may add more, but not change these.

```python
# builds.py
class InvalidIPA(Exception): ...               # not an .ipa, or no bundle id in it
@dataclass
class Build:                                   # meta.json's fields, in snake_case
    key: str; fingerprint: str; app_id: str; name: str; bundle_id: str
    version: str; build: str; size: int; built_at: str; last_used_at: str
    duration: Optional[float]; source: str; configuration: Optional[str]
    profile: Optional[Dict[str, Any]]; has_icon: bool; published: bool
    path: Path                                 # its folder
    ipa_path: Path                             # property: path / "App.ipa"
    icon_path: Optional[Path]                  # property: path / "icon.png", if has_icon
    def installable(self, now: Optional[datetime] = None) -> bool: ...
list_builds(app_id) -> List[Build]             # newest built_at first; skips unreadable ones
get(app_id, key) -> Optional[Build]
find(app_id, fingerprint: str) -> Optional[Build]
latest(app_id) -> Optional[Build]              # newest installable
store(app: App, ipa: Path, fingerprint: Fingerprint, duration: float) -> Build   # moves/copies ipa in, prunes
publish(app_id: str, ipa: Path, name: Optional[str] = None) -> Build              # copies; doesn't touch config
mark_used(build: Build) -> None
delete_all(app_id) -> None

# builder.py
class Job:
    app: App; state: str; step: str; error: Optional[str]; build: Optional[Build]
    queued_at: float; started_at: Optional[float]; finished_at: Optional[float]
    fingerprint: Optional[Fingerprint]
class Builder:
    def __init__(self, start_worker: bool = True): ...
    def request(self, app: App, force: bool = False) -> Union[Job, Build]: ...   # Build = cache hit
    def job(self, app_id: str) -> Optional[Job]: ...
    def wait(self, job: Job, timeout: Optional[float] = None,
             on_step: Optional[Callable[[str], None]] = None) -> Job: ...      # until done or failed
    def cancel(self, app_id: str) -> bool: ...

# xcode_build.py
BUILDER_VERSION: int
class BuildError(Exception): ...               # its message says what to do
find_project(folder: Path) -> Path             # the .xcworkspace or .xcodeproj the builder would use
find_scheme(project: Path, preferred: Optional[str] = None) -> str
main(argv=None) -> int

# server.py
make_server(host: Optional[str] = None, port: Optional[int] = None) -> ThreadingHTTPServer
serve(host: Optional[str] = None, port: Optional[int] = None) -> None
```

## HTTP API (`server.py`)

Stdlib `ThreadingHTTPServer` on `config.host:config.port`. JSON responses have
`Cache-Control: no-store`. Unknown app ids are 404 with `{"error": "…"}`. HEAD returns headers
only, never a body (the old server streamed whole `.ipa` files in answer to HEAD).

The public base URL is `config.url`, else `https://<Host header>` (`http://` for localhost and
127.0.0.1, for local testing). Install links:
`itms-services://?action=download-manifest&url=<base>/apps/<id>/<key>/manifest.plist` (URL-encoded).
Build URLs contain the key, so they never need cache-busting.

### Objects

**Build**: a stored build, as the page needs it:

```json
{
  "key": "a1b2c3d4e5f60718", "version": "1.0", "build": "1", "size": 3014644,
  "builtAt": "…", "duration": 41.2, "source": "main@f2d6080+changes", "configuration": "Debug",
  "bundleId": "com.example.TrailMap",
  "expiresAt": "…" | null, "devices": 3 | null, "installable": true,
  "installUrl": "itms-services://…", "iconUrl": "/apps/trailmap/a1b2…/icon.png" | null
}
```

**Status**: an app's build activity:

```json
{
  "state": "idle" | "queued" | "building" | "done" | "failed",
  "step": "Building TrailMap",   // queued: "Waiting for <other app>" or "Waiting"
  "elapsed": 12,               // seconds queued, or building, or that the build took
  "error": "…",                // failed only
  "cached": true,              // done, from a cache hit (POST /install only)
  "build": { Build }           // done only: the build to install
}
```

**App**:

```json
{
  "id": "trailmap",
  "name": "TrailMap",                    // latest build's name, else the config's
  "bundleId": "com.example.TrailMap",  // latest build's, else null
  "buildable": true,
  "freshness": "current" | "changed" | "unbuilt" | null,
  "problem": "Folder not found: …" | null,
  "latest": { Build } | null,          // newest installable build by builtAt
  "status": { Status },
  "iconUrl": "/apps/trailmap/icon.png" | null
}
```

`freshness` compares the current fingerprint with the stored builds: `current` if one with that
fingerprint is installable (a tap installs at once), `changed` if there are builds but none
matches, `unbuilt` if there are none, null if not buildable or the fingerprint failed (then
`problem` says why). `/api/apps` fingerprints apps in parallel and caches each result for 2 seconds.

### Routes

| Method, path | Answer |
|---|---|
| `GET /` | `web/index.html` |
| `GET /static/<file>` | a file from `web/` (no subfolders, no `..`) |
| `GET /favicon.ico` | `web/favicon-32.png` |
| `GET /api/apps` | `{"title": "Apps", "apps": [App…]}`, newest `latest.builtAt` first, unbuilt last |
| `GET /api/apps/<id>` | App, plus `"commits": [{"sha","subject","date"}]`, `"info": {"dir","project","scheme","configuration","command"}` (paths with `~`), `"builds": [Build…]` (newest first) |
| `POST /api/apps/<id>/install[?force=1]` | Status, from `Builder.request`. 409 with `{"error"}` if the app isn't buildable and has no installable build. For an app that isn't buildable, it's the latest build, as `done` |
| `GET /api/apps/<id>/status` | Status |
| `GET /api/apps/<id>/log` | the last build's log as `text/plain; charset=utf-8`, its last 256 KB; 404 if none |
| `GET /apps/<id>/icon.png` | the latest build's icon; 404 if none |
| `GET /apps/<id>/<key>/manifest.plist` | the OTA manifest (`software-package` and, with an icon, `display-image` and `full-size-image`; metadata `bundle-identifier`, `bundle-version` = version, `kind` = software, `title` = name) |
| `GET /apps/<id>/<key>/App.ipa` | the `.ipa` as `application/octet-stream` |
| `GET /apps/<id>/<key>/icon.png` | the build's icon |

The server logs requests except `/api/` ones (the page polls them) and ignores clients that hang
up mid-download (`BrokenPipeError`, `ConnectionResetError`) instead of printing a traceback.

`server.serve(host=None, port=None)` runs it until interrupted; `server.make_server(host, port)`
returns the server without running it, for tests.

## The page (`web/`)

One page, `index.html` with `app.css` and `app.js`, no build step, no frameworks, for Safari on
iOS 17 and later. It has the look of the iOS 5 App Store: the blue-grey navigation bar with
its segmented control, grey gradient rows, glossy rounded icons, the FREE → INSTALL two-tap
button, the twelve-bar spinner and blue progress bar while building, the black glossy tab bar
with a red badge.

- **List** (Featured tab): every app. The detail line says `Up to date · 1.0 (1) · 3.0 MB ·
  main@f2d6080`, or `Changed since last build`, or `Not built yet`, or the problem. Apps that are
  building show the spinner, progress bar and `Building TrailMap… 0:12`.
- **Updates tab**: apps whose `freshness` is `changed`, with its count as the badge. On those, the
  price button says UPDATE instead of FREE, as updates did in the iOS 5 store.
- **Installing**: tapping the price arms it (green INSTALL); tapping again POSTs `install`. A `done`
  answer goes straight to `build.installUrl` (`location.href`); otherwise it polls `status` every
  2 seconds until done or failed, riding out a phone that sleeps or drops off the network for
  up to a minute. A failure turns the button red (RETRY) and shows the error. A tap always asks
  the store, even after a build finished on this page: the sources may have moved on since.
- **Detail view** (`#/apps/<id>`, opened by tapping a row outside its button): an iOS 5 style
  navigation bar with a back button, a header with the large icon, name, bundle id and the same
  install button; then grouped sections: What's New (the recent commits), Information (Version,
  Size, Configuration, Source, Built, Build time, Profile expires, Devices, Folder, Scheme or
  Command), the stored builds (each installable), View Build Log, and Rebuild (a forced build,
  for when a phone was added to the team after the last build, as the cached one won't
  install on it).
- It refreshes `/api/apps` every 10 seconds while visible, and follows builds started elsewhere.

## The CLI (`cli.py`)

`ota-store <command>`, also `python3 -m ota_store`:

| Command | Does |
|---|---|
| `serve [--host H] [--port P]` | runs the server in the foreground |
| `add [DIR] [--id] [--name] [--project] [--scheme] [--configuration] [--command] [--input PATH]… [--icon]` | registers an app (DIR defaults to `.`). Without `--command`, finds the project and scheme as the Xcode builder would, and stores them; id and name default from the scheme |
| `list` | apps with their freshness and latest build |
| `remove ID [--purge]` | unregisters; `--purge` also deletes its builds, log and DerivedData |
| `build ID [--force]` | asks the running server to build (as a tap would), following its steps in the terminal; exits non-zero on failure. If no server answers, builds in-process |
| `publish IPA --id ID [--name N]` | stores an `.ipa` made elsewhere (registers the app without a `dir` if needed) |
| `log ID` | prints the last build's log |
| `xcode-build` | the built-in Xcode builder (for build commands) |
| `service install\|uninstall\|start\|stop\|restart\|status` | the LaunchAgent `local.ota-store` running `serve`, logging to `server.log` |
| `doctor` | checks Python, Xcode, the config, each app's folder, the server, `tailscale serve`, the public URL |
| `url` | the URL phones open |

## Tests

`/usr/bin/python3 -m unittest discover -s tests -t .` from the repository root, standard library
only, no network, no Xcode (except tests that skip themselves when `OTA_TEST_XCODE` isn't set).
Use `tests.helpers`: `temp_store()` for isolated state, `make_ipa()` for `.ipa` files,
`fake_build_command()` for apps whose builds run `tests/fake_build.py`.
