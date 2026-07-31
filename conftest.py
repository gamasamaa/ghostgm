"""Put the repo root on sys.path so tests can `import ghost` / `import prep`."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
