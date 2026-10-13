#!/usr/bin/env python3
"""Run directly on the host; no pip dependencies or running services required."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "api"))

from gsm_api.server_inventory import main


if __name__ == "__main__":
    raise SystemExit(main())
