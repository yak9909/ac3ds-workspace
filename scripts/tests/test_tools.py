import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import audit
import check_derived
import fetch_tools
import sync_skills
import wslib
from conftest import FAKE_EMAIL, FAKE_TOKEN, FAKE_USERPATH, REAL_ROOT


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """repos に re だけがある小さな作業場。"""
    (tmp_path / "workspace.toml").write_text(
        'data = "_local"\n[repos.re]\nrole = "analysis"\n[local_only]\nfiles = ["workspace.local.toml"]\n',
        encoding="utf-8")
    (tmp_path / "re" / "skills" / "alpha").mkdir(parents=True)
    (tmp_path / "re" / "skills" / "alpha" / "SKILL.md").write_text("a\n", encoding="utf-8")
    (tmp_path / "skills" / "beta").mkdir(parents=True)
    (tmp_path / "skills" / "beta" / "SKILL.md").write_text("b\n", encoding="utf-8")
    monkeypatch.setenv("AC3DS_WORKSPACE", str(tmp_path))
    return tmp_path


# ---- audit ----
def test_check_outgoing_positive():
    assert audit.check_outgoing("連絡は " + FAKE_EMAIL)
    assert audit.check_outgoing("token " + FAKE_TOKEN)
    assert audit.check_outgoing("path " + FAKE_USERPATH)


def test_check_outgoing_negative():
    assert audit.check_outgoing("関数 sub_123456 は 0x00100000 を読む") == []


def test_scatter(ws):
    (ws / "README.md").write_text("x", encoding="utf-8")
    (ws / "_local").mkdir()
    (ws / "re").mkdir(exist_ok=True)
    (ws / "workspace.local.toml").write_text("", encoding="utf-8")
    assert audit.scatter_warnings(ws) == []
    (ws / "stray.txt").write_text("", encoding="utf-8")
    w = audit.scatter_warnings(ws)
    assert len(w) == 1 and "stray.txt" in w[0]


def test_scan_file(ws):
    mod = wslib.load_re_audit(REAL_ROOT)
    assert audit.scan_file(mod, "scripts/a.py", b"x = 1\n") == []
    assert audit.scan_file(mod, "other/a.py", b"x = 1\n")
    assert audit.scan_file(mod, "README.md", b"a\r\nb\n")


# ---- sync_skills ----
def test_sync_adds_and_removes(ws):
    sync_skills.sync(ws)
    for loc, name in ((ws, "beta"), (ws / "re", "alpha")):
        for t in sync_skills.TARGET_DIRS:
            assert (loc / t / name / "SKILL.md").is_file()
    assert sync_skills.check(ws) == []
    stale = ws / ".claude" / "skills" / "gone"
    stale.mkdir()
    (stale / "SKILL.md").write_text("x", encoding="utf-8")
    assert any("本体に無い" in m for m in sync_skills.check(ws))
    sync_skills.sync(ws)
    assert not stale.exists()
    assert sync_skills.check(ws) == []


def test_check_mismatch_and_missing(ws):
    assert any("写しが無い" in m for m in sync_skills.check(ws))
    sync_skills.sync(ws)
    (ws / ".agents" / "skills" / "beta" / "SKILL.md").write_text("changed\n", encoding="utf-8")
    assert any("一致しない" in m for m in sync_skills.check(ws))
    sync_skills.sync(ws)
    assert sync_skills.check(ws) == []


# ---- fetch_tools ----
def make_zip(path: Path, content: bytes):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("tool.exe", content)


def spec_for(zpath: Path, exe_bytes: bytes, exe_hash=None):
    return {"version": "1.0", "url": zpath.as_uri(), "exe": "tool.exe",
            "sha256": hashlib.sha256(zpath.read_bytes()).hexdigest(),
            "exe_sha256": exe_hash or hashlib.sha256(exe_bytes).hexdigest()}


def test_fetch_ok_then_idempotent(tmp_path):
    z = tmp_path / "t.zip"
    make_zip(z, b"hello")
    spec = spec_for(z, b"hello")
    tools = tmp_path / "tools"
    assert fetch_tools.ensure_tool("t", spec, tools) == "fetched"
    assert (tools / "t" / "1.0" / "tool.exe").read_bytes() == b"hello"
    assert (tools / "t" / "1.0" / "download" / "t.zip").exists()
    assert fetch_tools.ensure_tool("t", spec, tools) == "ok"


def test_fetch_zip_hash_mismatch_keeps_file(tmp_path):
    z = tmp_path / "t.zip"
    make_zip(z, b"hello")
    spec = spec_for(z, b"hello")
    spec["sha256"] = "0" * 64
    tools = tmp_path / "tools"
    with pytest.raises(fetch_tools.FetchError):
        fetch_tools.ensure_tool("t", spec, tools)
    assert (tools / "t" / "1.0" / "download" / "t.zip").exists()


def test_fetch_exe_hash_mismatch(tmp_path):
    z = tmp_path / "t.zip"
    make_zip(z, b"hello")
    spec = spec_for(z, b"hello", exe_hash="0" * 64)
    tools = tmp_path / "tools"
    with pytest.raises(fetch_tools.FetchError):
        fetch_tools.ensure_tool("t", spec, tools)
    assert (tools / "t" / "1.0" / "tool.exe").exists()


# ---- check_derived ----
def test_derived_positive():
    for t in ("see ACNL-F0123", "see HHD-F12345", "uses ac3ds-re", "read findings/x.md", "see topics/y",
              "mail " + FAKE_EMAIL, FAKE_TOKEN):
        assert check_derived.check_text("a.md", t, wslib.load_re_audit(REAL_ROOT)), t


def test_derived_negative():
    mod = wslib.load_re_audit(REAL_ROOT)
    assert check_derived.check_text("a.md", "ゲームの事実: 家具は 0x10 バイト。ACNL-F12 は短い。", mod) == []
