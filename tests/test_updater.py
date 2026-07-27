"""Self-updater: version logic, skip rules, payload safety, rollback."""

import io
import subprocess
import zipfile
from types import SimpleNamespace

import livery_tracker.updater as updater
from livery_tracker.updater import (
    Update,
    _extract_payload,
    apply_update,
    auto_update_enabled,
    check_for_update,
    parse_version,
)


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


GOOD_PAYLOAD = {
    "repo-abc123/livery_tracker/__init__.py": '__version__ = "9.9.9"\n',
    "repo-abc123/livery_tracker/geo.py": "# new code\n",
    "repo-abc123/requirements.txt": "httpx>=0.27\n",
}


# -- version parsing -----------------------------------------------------------

def test_parse_version():
    assert parse_version("1.2.3") == (1, 2, 3)
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("v2.0") == (2, 0)
    assert parse_version("not-a-version") is None
    assert parse_version("v1.2.3-beta") is None


# -- check_for_update ----------------------------------------------------------

def fake_response(status=200, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body or {}, content=b"")


def test_check_handles_no_releases(monkeypatch):
    monkeypatch.setattr(updater.httpx, "get", lambda *a, **k: fake_response(404))
    assert check_for_update("1.0.0") is None


def test_check_ignores_older_or_equal(monkeypatch):
    monkeypatch.setattr(
        updater.httpx, "get",
        lambda *a, **k: fake_response(200, {"tag_name": "v1.0.0", "zipball_url": "u"}),
    )
    assert check_for_update("1.0.0") is None
    assert check_for_update("1.2.0") is None


def test_check_returns_newer(monkeypatch):
    monkeypatch.setattr(
        updater.httpx, "get",
        lambda *a, **k: fake_response(200, {"tag_name": "v1.2.0", "zipball_url": "zip"}),
    )
    found = check_for_update("1.1.0")
    assert found is not None
    assert found.tag == "v1.2.0" and found.zipball_url == "zip"


def test_check_survives_network_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(updater.httpx, "get", boom)
    assert check_for_update("1.0.0") is None


# -- skip rules ----------------------------------------------------------------

def test_auto_update_skips_git_checkouts(tmp_path):
    (tmp_path / ".git").mkdir()
    assert not auto_update_enabled(tmp_path)


def test_auto_update_respects_env_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("LT_AUTO_UPDATE", "0")
    assert not auto_update_enabled(tmp_path)


def test_auto_update_enabled_for_plain_installs(tmp_path, monkeypatch):
    monkeypatch.delenv("LT_AUTO_UPDATE", raising=False)
    assert auto_update_enabled(tmp_path)


# -- payload sanity ------------------------------------------------------------

def test_extract_rejects_garbage(tmp_path):
    assert _extract_payload(b"this is not a zip", tmp_path / "a") is None


def test_extract_rejects_payload_without_package(tmp_path):
    dest = tmp_path / "b"
    dest.mkdir()
    payload = make_zip({"repo-x/README.md": "hi"})
    assert _extract_payload(payload, dest) is None


def test_extract_accepts_good_payload(tmp_path):
    dest = tmp_path / "c"
    dest.mkdir()
    assert _extract_payload(make_zip(GOOD_PAYLOAD), dest) is not None


# -- apply_update --------------------------------------------------------------

def install_root(tmp_path):
    root = tmp_path / "install"
    (root / "livery_tracker").mkdir(parents=True)
    (root / "livery_tracker" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    (root / "livery_tracker" / "old.py").write_text("old\n")
    (root / "requirements.txt").write_text("httpx>=0.27\n")
    (root / "data").mkdir()
    (root / "data" / "config_and_watch.json").write_text("{}")
    (root / ".env").write_text("BOT_TOKEN=x\n")
    return root


def test_apply_update_swaps_code_and_keeps_state(tmp_path, monkeypatch):
    root = install_root(tmp_path)
    monkeypatch.setattr(
        updater.httpx, "get",
        lambda *a, **k: SimpleNamespace(status_code=200, content=make_zip(GOOD_PAYLOAD)),
    )
    ok = apply_update(Update("v9.9.9", (9, 9, 9), "url"), root=root)
    assert ok
    assert '"9.9.9"' in (root / "livery_tracker" / "__init__.py").read_text()
    assert not (root / "livery_tracker" / "old.py").exists()
    # State untouched, scratch dirs cleaned up.
    assert (root / ".env").read_text() == "BOT_TOKEN=x\n"
    assert (root / "data" / "config_and_watch.json").exists()
    assert not (root / ".update-staging").exists()
    assert not (root / ".update-backup").exists()


def test_apply_update_rejects_bad_payload(tmp_path, monkeypatch):
    root = install_root(tmp_path)
    monkeypatch.setattr(
        updater.httpx, "get",
        lambda *a, **k: SimpleNamespace(status_code=200, content=b"garbage"),
    )
    assert not apply_update(Update("v9.9.9", (9, 9, 9), "url"), root=root)
    assert '"1.0.0"' in (root / "livery_tracker" / "__init__.py").read_text()


def test_apply_update_rolls_back_when_pip_fails(tmp_path, monkeypatch):
    root = install_root(tmp_path)
    payload = dict(GOOD_PAYLOAD)
    payload["repo-abc123/requirements.txt"] = "httpx>=0.27\nnew-dep==1.0\n"
    monkeypatch.setattr(
        updater.httpx, "get",
        lambda *a, **k: SimpleNamespace(status_code=200, content=make_zip(payload)),
    )
    monkeypatch.setattr(
        updater.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, returncode=1, stdout="", stderr="no such dep"),
    )
    assert not apply_update(Update("v9.9.9", (9, 9, 9), "url"), root=root)
    # Original code and requirements restored.
    assert '"1.0.0"' in (root / "livery_tracker" / "__init__.py").read_text()
    assert (root / "livery_tracker" / "old.py").exists()
    assert (root / "requirements.txt").read_text() == "httpx>=0.27\n"
