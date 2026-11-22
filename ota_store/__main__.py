"""`python3 -m ota_store`: the same as the `ota-store` launcher."""

from __future__ import annotations

import sys

from .cli import main

sys.exit(main())
