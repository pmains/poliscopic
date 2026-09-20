#!/usr/bin/env python3
"""Read-only Stage 0 knowledge-graph integrity snapshot."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import get_engine
from scripts.entities.detect_entities import _integrity_snapshot


def main() -> None:
    print(json.dumps(_integrity_snapshot(get_engine()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
