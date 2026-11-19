"""Internal project ids (Q11, Q15, ...) are developer names, never product text (operator, 2026-11-18)."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTERNAL = re.compile(r"\bQ\d{1,2}\b|\bUI-PROTO-\d+\b")


def _visible(html: str) -> str:
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


def test_dashboard_shows_no_internal_ids():
    html = _visible((ROOT / "src/fleet/api/dashboard.html").read_text(encoding="utf-8"))
    assert not INTERNAL.findall(html)


def test_cli_help_shows_no_internal_ids():
    out = subprocess.run([sys.executable, "-m", "fleet.api.cli", "--help"], capture_output=True, text=True,
                         cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    text = out.stdout + out.stderr
    for sub in ("routine", "signer", "privateer"):
        r = subprocess.run([sys.executable, "-m", "fleet.api.cli", sub, "--help"], capture_output=True,
                           text=True, cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
        text += r.stdout + r.stderr
    assert "usage" in text.lower()
    assert not INTERNAL.findall(text)


def test_readme_shows_no_internal_ids():
    assert not INTERNAL.findall((ROOT / "README.md").read_text(encoding="utf-8"))


def test_ui_preview_shows_no_internal_ids():
    page = ROOT / "src/fleet/api/static/preview/index.html"
    assert not INTERNAL.findall(_visible(page.read_text(encoding="utf-8")))
