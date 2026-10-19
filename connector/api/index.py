"""Vercel entry point: every path is rewritten here (see vercel.json)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from huckleberry_mcp.app import create_app  # noqa: E402

app = create_app()
