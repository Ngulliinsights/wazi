"""'A repository that does not run' is a disqualifier. This once failed on a clean machine (flat-layout discovery error)."""
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_every_package_directory_is_declared_and_the_ui_ships_with_it():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(cfg["tool"]["setuptools"]["packages"])
    found = {".".join(p.parent.relative_to(ROOT).parts) for p in (ROOT / "wazi").rglob("__init__.py")}
    assert found <= declared, f"undeclared packages would be missing from an install: {found - declared}"
    assert "static/*.html" in cfg["tool"]["setuptools"]["package-data"]["wazi"] and cfg["build-system"]["build-backend"]
    assert (ROOT / "wazi" / "static" / "index.html").exists() and (ROOT / ".gitignore").exists() and (ROOT / "run.py").exists()


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("WAZI_TEST_INSTALL") != "1", reason="needs network and ~30s: set WAZI_TEST_INSTALL=1")
def test_a_fresh_copy_bootstraps_itself_with_one_command(tmp_path):
    import shutil
    dst = tmp_path / "repo"; shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns("var", ".venv", "__pycache__", ".pytest_cache", "*.zip"))
    out = subprocess.run([sys.executable, "run.py", "--check"], cwd=dst, capture_output=True, text=True, timeout=600)
    assert out.returncode == 0 and "check: OK" in out.stdout, out.stdout + out.stderr
