# ota-store

Install the iOS app your coding agent just changed. Open the store on your
iPhone and tap Install. Your Mac builds the working copy, including uncommitted
edits, and reuses the last build when the source hasn't changed. Your team can
try changes on a phone without a TestFlight upload.

## Setup

Use a Mac with Xcode and Python 3.9 or newer. Sign in to Xcode with your
development team and register the iPhones that will install the builds. Connect
the Mac and iPhones to the same Tailscale network.

From this checkout on the Mac, run:

```sh
./ota-store service install
tailscale serve --bg --https=443 http://127.0.0.1:8765
./ota-store url
```

Open the URL in Safari on your iPhone. Add it to your Home Screen if you like.
Keep the Mac awake and logged in while you use the store.

Register an app from the folder that contains its Xcode project or workspace:

```sh
/path/to/ota-store/ota-store add
```

Tap Install on the phone, or have your agent run `./ota-store build <id>` from
this checkout when it finishes a change. Run `./ota-store list` to find app IDs
and `./ota-store doctor` if setup gets stuck.

Anyone who can reach the store can download builds and start new ones. Use
Tailscale Serve with access limited to your team and keep Funnel off.
See [AGENTS.md](AGENTS.md) for an agent-led setup and [docs/DESIGN.md](docs/DESIGN.md)
for custom build commands and API details.

## Demo

Run `python3 tests/mock_server.py` and open `http://127.0.0.1:8799`. The demo
uses fictional apps and simulates builds and installs. For screenshots on an
iPhone, point a private Tailscale Serve HTTPS endpoint at this port.

MIT licensed. See [LICENSE](LICENSE).
