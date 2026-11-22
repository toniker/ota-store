# Set up ota-store on a tailnet

Run setup on the Mac that has the iOS app checkout, under the logged-in user
who builds the app. Use a stable path for this clone since launchd runs the
launcher from it.

1. Run `/usr/bin/python3 --version` and `xcodebuild -version`. The LaunchAgent
   uses the system Python, which must be 3.9 or newer. Have the user sign in to
   Xcode, select their development team, and register each test iPhone. Verify
   the app's device signing and scheme in Xcode before debugging the store.
2. Check `tailscale status` and `tailscale serve status`. The Mac and test
   iPhones need access to the same tailnet. Keep any existing Serve routes. If
   HTTPS port 443 already serves another app, use a free HTTPS port such as
   8443. Use the root path because the store's API and assets use absolute paths.
3. Run `./ota-store service install`. Verify
   `curl -fsS http://127.0.0.1:8765/api/apps` returns JSON. Keep the backend on
   loopback. Use `./ota-store service status` and
   `~/Library/Application Support/ota-store/logs/server.log` if it fails.
4. Run `tailscale serve --bg --https=443 http://127.0.0.1:8765` with the free
   HTTPS port from step 2. If Tailscale prompts for HTTPS certificate setup,
   have the tailnet admin finish it. Keep Funnel off and scope access to the
   people who may trigger builds and download IPAs. The store has no app login.
   Check `./ota-store url` and fetch `/api/apps` over that HTTPS URL.
5. Register the iOS checkout with
   `./ota-store add /path/to/ios --project Example.xcworkspace --scheme Example --id example`.
   Use the team's actual path, project and device scheme. Pass `--input PATH`
   for build inputs outside the app folder. For Tuist, XcodeGen or other custom
   build steps, read the build command contract in [docs/DESIGN.md](docs/DESIGN.md#the-build-command-contract).
6. Run `./ota-store doctor` and `./ota-store build example`. On a registered
   iPhone connected to the tailnet, open the HTTPS URL in Safari and tap
   Install. The setup is verified when the app opens on that iPhone. If a phone
   was added after the last build, use Rebuild to refresh the signing profile.

The store keeps config, IPAs and logs in
`~/Library/Application Support/ota-store` and DerivedData in
`~/Library/Caches/ota-store`. Keep machine-specific config, signing material
and real build data outside Git. Use fictional data for screenshots and tests.

For changes to the store, run `/usr/bin/python3 -m unittest discover -s tests -t .`.
Run `python3 tests/mock_server.py` to exercise the web UI without Xcode.
