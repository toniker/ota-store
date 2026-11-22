"""A build command that follows the store's contract without Xcode.

Writes an .ipa to $OTA_OUTPUT and reports steps with `==> ` lines. Its
behaviour comes from environment variables:

    FAKE_BUNDLE_ID   bundle identifier (default com.example.Fake)
    FAKE_NAME        app name (default Fake)
    FAKE_VERSION     CFBundleShortVersionString (default 1.0)
    FAKE_BUILD       CFBundleVersion (default 1)
    FAKE_SLEEP       seconds to spend "building" (default 0)
    FAKE_FAIL        if set, print an Xcode-style error and exit 65
    FAKE_NO_OUTPUT   if set, exit 0 without writing the .ipa
    FAKE_EXPIRED     if set, the profile in the .ipa expired yesterday
    FAKE_COUNTER     a file: each run appends a line, so tests can count builds
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers import make_ipa, png  # noqa: E402


def main() -> int:
    env = os.environ
    if env.get("FAKE_COUNTER"):
        with open(env["FAKE_COUNTER"], "a") as counter:
            counter.write(f"{env.get('OTA_APP_ID', '')}\n")
    print("==> Preparing", flush=True)
    print(f"cwd={os.getcwd()} derived={env.get('OTA_DERIVED_DATA', '')}", flush=True)
    print("==> Building Fake", flush=True)
    time.sleep(float(env.get("FAKE_SLEEP") or 0))
    if env.get("FAKE_FAIL"):
        print("/tmp/Fake/ContentView.swift:12:5: error: cannot find 'Foo' in scope", flush=True)
        print("** BUILD FAILED **", flush=True)
        return 65
    if env.get("FAKE_NO_OUTPUT"):
        return 0
    print("==> Packaging", flush=True)
    expires = datetime.now(timezone.utc) + (timedelta(days=-1) if env.get("FAKE_EXPIRED") else timedelta(days=300))
    make_ipa(Path(env["OTA_OUTPUT"]), bundle_id=env.get("FAKE_BUNDLE_ID", "com.example.Fake"),
             name=env.get("FAKE_NAME", "Fake"), version=env.get("FAKE_VERSION", "1.0"),
             build=env.get("FAKE_BUILD", "1"), expires=expires, icon=png(4, 4))
    print("Built", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
