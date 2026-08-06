"""The bot has no packages — modules resolve because the Dockerfile puts
`src/HirezAPI` and `src/ml` directly on PYTHONPATH, so `import build_features`
works from anywhere inside the image. Tests need the same layout.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for relative in ("src/HirezAPI", "src/ml", "src/SmiteBot", "src/match_data_collector"):
    path = os.path.join(ROOT, relative)
    if path not in sys.path:
        sys.path.insert(0, path)
