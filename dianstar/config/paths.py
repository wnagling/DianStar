"""Local data paths, independent of model and UI modules."""
import sys
from pathlib import Path


PROJECT_ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parents[2])


CONFIG_FILE = PROJECT_ROOT / ".local" / "config.json"


ENV_FILE = PROJECT_ROOT / ".env"
