"""Absolute-path launcher; works regardless of the MCP client's working folder."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from localmind.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
