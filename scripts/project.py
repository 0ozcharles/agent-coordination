"""Use the project CLI directly from a checkout, without installing dependencies."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_rsi.cli import main

raise SystemExit(main())

