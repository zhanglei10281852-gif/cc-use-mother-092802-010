#!/usr/bin/env python3
"""根目录启动入口：python serve.py --db data/policy.json --port 8000"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from transition_policy.api import main  # noqa: E402

if __name__ == "__main__":
    main()
