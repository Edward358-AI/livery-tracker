"""Self-update from GitHub Releases.

The update channel is *releases*, not main — a release is the deliberate
"friends can have this" gate. The updater downloads the release zipball,
sanity-checks it, swaps in the new code with a backup to roll back to, and
asks the supervisor to restart the process via exit code 42.

Auto-update is skipped for git checkouts (developers manage themselves),
inside Docker (image-based updates), and when LT_AUTO_UPDATE=0.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import __version__
from .config import PROJECT_ROOT
from .web import USER_AGENT

log = logging.getLogger(__name__)

REPO = "Edward358-AI/livery-tracker"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

RESTART_EXIT_CODE = 42

# What an update is allowed to replace. State (data/, .env) is never touched.
CODE_PATHS = [
    "livery_tracker",
    "requirements.txt",
    "install.ps1",
    "install.sh",
    "runner.ps1",
    "runner.sh",
    "tracker.sh",
    "livery-tracker.service",
    "README.md",
    "LICENSE",
]


def parse_version(text: str) -> tuple[int, ...] | None:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3); None if unparseable."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def auto_update_enabled(root: Path | None = None) -> bool:
    root = root or PROJECT_ROOT
    if os.environ.get("LT_AUTO_UPDATE", "").strip() == "0":
        return False
    if (root / ".git").exists():
        return False  # developer checkout — git manages this copy
    if Path("/.dockerenv").exists():
        return False  # container — update the image instead
    return True


@dataclass
class Update:
    tag: str
    version: tuple[int, ...]
    zipball_url: str


def check_for_update(current: str = __version__) -> Update | None:
    """The newest release strictly ahead of the running version, else None."""
    current_v = parse_version(current)
    if current_v is None:
        return None
    try:
        resp = httpx.get(
            LATEST_RELEASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
            timeout=30,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Update check failed: %s", exc)
        return None
    if resp.status_code != 200:  # 404 = no releases yet — nothing to do
        return None
    body = resp.json()
    tag = body.get("tag_name") or ""
    remote_v = parse_version(tag)
    if remote_v is None or remote_v <= current_v:
        return None
    return Update(tag=tag, version=remote_v, zipball_url=body.get("zipball_url") or "")


def _extract_payload(zip_bytes: bytes, dest: Path) -> Path | None:
    """Unpack a GitHub zipball (single top-level dir) and sanity-check it."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(dest)
    except zipfile.BadZipFile:
        log.error("Update payload is not a valid zip")
        return None
    roots = [p for p in dest.iterdir() if p.is_dir()]
    if len(roots) != 1:
        log.error("Update payload has unexpected layout")
        return None
    payload = roots[0]
    init = payload / "livery_tracker" / "__init__.py"
    if not init.exists():
        log.error("Update payload is missing livery_tracker/__init__.py")
        return None
    version_match = re.search(r"__version__\s*=\s*\"([^\"]+)\"", init.read_text(encoding="utf-8"))
    if not version_match or parse_version(version_match.group(1)) is None:
        log.error("Update payload has no parseable version")
        return None
    return payload


def apply_update(update: Update, root: Path | None = None) -> bool:
    """Download and install a release. True on success (caller should restart).

    On any failure the previous code is restored from the backup and the
    running installation is left untouched.
    """
    root = root or PROJECT_ROOT
    try:
        resp = httpx.get(
            update.zipball_url,
            headers={"User-Agent": USER_AGENT},
            timeout=120,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            log.error("Update download failed: HTTP %s", resp.status_code)
            return False
        zip_bytes = resp.content
    except Exception as exc:  # noqa: BLE001
        log.error("Update download failed: %s", exc)
        return False

    staging = root / ".update-staging"
    backup = root / ".update-backup"
    for scratch in (staging, backup):
        shutil.rmtree(scratch, ignore_errors=True)
    staging.mkdir()

    payload = _extract_payload(zip_bytes, staging)
    if payload is None:
        shutil.rmtree(staging, ignore_errors=True)
        return False

    old_requirements = (root / "requirements.txt").read_text(encoding="utf-8") \
        if (root / "requirements.txt").exists() else ""

    # Back up current code, then swap in the new tree.
    backup.mkdir()
    replaced: list[str] = []
    try:
        for name in CODE_PATHS:
            src = payload / name
            cur = root / name
            if not src.exists():
                continue
            if cur.exists():
                shutil.move(str(cur), str(backup / name))
            if src.is_dir():
                shutil.copytree(src, cur)
            else:
                shutil.copy2(src, cur)
            replaced.append(name)

        new_requirements = (root / "requirements.txt").read_text(encoding="utf-8") \
            if (root / "requirements.txt").exists() else ""
        if new_requirements != old_requirements:
            log.info("requirements.txt changed — installing dependencies...")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-r",
                 str(root / "requirements.txt")],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                raise RuntimeError(f"pip install failed: {result.stderr[-500:]}")

        log.info("Updated to %s (%s)", update.tag, ", ".join(replaced))
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Update failed (%s) — rolling back", exc)
        for name in replaced:
            cur = root / name
            if cur.exists():
                shutil.rmtree(cur, ignore_errors=True) if cur.is_dir() else cur.unlink()
            saved = backup / name
            if saved.exists():
                shutil.move(str(saved), str(cur))
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
