import sys
from pathlib import Path

# Backend modules import each other by bare name (as inside the Docker container).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
