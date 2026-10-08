#!/usr/bin/env python3
"""Regenerate dolphin/tools/tools.json from the backend allowlist (single source of truth)."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.dolphin.tools import openai_tools  # noqa: E402

out = ROOT / "dolphin" / "tools" / "tools.json"
out.write_text(json.dumps(openai_tools(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(openai_tools())} tools)")
