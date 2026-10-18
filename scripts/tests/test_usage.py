import io
import json
import os
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import capacity
import orch
import statusline
import usage_guard
import usage_watch


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_collect_rollout_and_expiry(tmp_path):
    state = tmp_path / "_local/state"
    write_json(state / "usage.json", {"claude": {
        "five_hour_used_percent": 82, "resets_at": 100, "updated": 50, "extra": "保持"}})
    sessions = tmp_path / "sessions"
    old = sessions / "old/rollout-old.jsonl"
    latest = sessions / "new/rollout-new.jsonl"
    write_json(old, {"type": "token_count", "rate_limits": {
        "primary": {"window_minutes": 300, "used_percent": 99}}})
    event = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"window_minutes": 300, "used_percent": 85, "resets_at": 200},
        "secondary": {"window_minutes": 10080, "used_percent": 91, "resets_at": 300}}}}
    write_json(latest, event)
    with latest.open("a", encoding="utf-8") as f:
        f.write('\n' + json.dumps({"type": "token_count", "rate_limits": {
            "primary": {"window_minutes": 300, "used_percent": 86, "resets_at": 200},
            "secondary": {"window_minutes": 10080, "used_percent": 92, "resets_at": 300}}}))
        f.write('\n{"type":"other"}\n{"broken":')
    os.utime(old, (1, 1))
    os.utime(latest, (2, 2))
    data = usage_watch.collect(tmp_path, sessions=sessions, now=100)
    assert data["claude"] == {"five_hour_used_percent": 0, "resets_at": 100, "updated": 50, "extra": "保持"}
    assert data["codex"] == {"five_hour_used_percent": 86, "resets_at": 200,
                             "weekly_used_percent": 92, "weekly_resets_at": 300, "updated": 100}
    data = usage_watch.collect(tmp_path, sessions=sessions, now=200)
    assert data["codex"]["five_hour_used_percent"] == 0
    assert data["codex"]["weekly_used_percent"] == 92
    assert usage_watch.collect(tmp_path, sessions=sessions, now=300)["codex"]["weekly_used_percent"] == 0


def test_statusline_fallback_and_preserves_codex(tmp_path):
    state = tmp_path / "_local/state"
    write_json(state / "usage.json", {"codex": {"five_hour_used_percent": 23}})
    write_json(state / "statusline_last.json", {"rate_limits": {
        "five_hour": {"used_percentage": 81, "resets_at": 9999999999}}})
    data = usage_watch.collect(tmp_path, sessions=tmp_path / "absent", now=100)
    assert data["claude"]["five_hour_used_percent"] == 81
    statusline.write_usage(tmp_path, 82, 9999999999)
    assert usage_watch.read_json(state / "usage.json")["codex"]["five_hour_used_percent"] == 23


def test_reverse_lines_crosses_blocks(tmp_path):
    path = tmp_path / "rollout-big.jsonl"
    lines = [json.dumps({"text": "日本語" * 20000}), '{"type":"token_count"}', "tail"]
    path.write_bytes("\n".join(lines).encode("utf-8"))
    assert [line.decode() for line in usage_watch.reverse_lines(path)] == list(reversed(lines))


@pytest.mark.parametrize("provider,agent,expected", [
    ("claude", None, "スキル wrapup"), ("claude", "worker-1", ".task/notes.md"),
    ("codex", None, ".task/notes.md")])
def test_guard_threshold_once_and_message(tmp_path, monkeypatch, capsys, provider, agent, expected):
    monkeypatch.setenv("AC3DS_GUARD_THRESHOLD", "80")
    monkeypatch.setattr(usage_guard.wslib, "workspace_root", lambda: tmp_path)
    usage = {provider: {"five_hour_used_percent": 79}}
    monkeypatch.setattr(usage_watch, "collect", lambda *_: usage)
    payload = {"session_id": "session-1", "hook_event_name": "PreToolUse"}
    if agent:
        payload["agent_id"] = agent
    monkeypatch.setattr(usage_guard.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert usage_guard.main(["--provider", provider]) == 0
    assert capsys.readouterr().out == ""
    usage[provider]["five_hour_used_percent"] = 80
    monkeypatch.setattr(usage_guard.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert usage_guard.main(["--provider", provider]) == 0
    raw = capsys.readouterr().out
    output = json.loads(raw)  # 余分な JSON・文字があれば失敗する
    assert len(raw.splitlines()) == 1
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert expected in output["hookSpecificOutput"]["additionalContext"]
    assert "80%" in output["hookSpecificOutput"]["additionalContext"]
    assert (tmp_path / "_local/state/guard" / (provider + "-session-1")).exists()
    payload["hook_event_name"] = "PostToolUse"
    assert usage_guard.guard(provider, payload, tmp_path) is None
    payload["session_id"] = "session-2"
    assert usage_guard.guard(provider, payload, tmp_path)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


@pytest.mark.parametrize("raw", ["", "{", "null", "[]", "1", '"string"', '{}'])
def test_guard_bad_input(monkeypatch, capsys, raw):
    monkeypatch.setattr(usage_guard.sys, "stdin", io.StringIO(raw))
    assert usage_guard.main(["--provider", "codex"]) == 0
    assert capsys.readouterr().out == ""


def test_guard_failure_is_silent(monkeypatch, capsys):
    monkeypatch.setattr(usage_guard.sys, "stdin", io.StringIO(
        '{"session_id":"s","hook_event_name":"PostToolUse"}'))
    def fail(*args):
        raise PermissionError("読めない")
    monkeypatch.setattr(usage_watch, "collect", fail)
    assert usage_guard.main(["--provider", "codex"]) == 0
    assert capsys.readouterr().out == ""


def test_threshold_config_and_override(tmp_path, monkeypatch):
    monkeypatch.delenv("AC3DS_GUARD_THRESHOLD", raising=False)
    (tmp_path / "workers.toml").write_text("[limits]\nstop_at_used_percent = 75\n", encoding="utf-8")
    assert usage_watch.threshold(tmp_path) == 75
    monkeypatch.setenv("AC3DS_GUARD_THRESHOLD", "1")
    assert usage_watch.threshold(tmp_path) == 1


@pytest.mark.parametrize("resume", [None, "thread-1"])
@pytest.mark.parametrize("idb", [None, "work.i64"])
def test_codex_hook_toml(resume, idb):
    root = (Path(__file__).resolve().parent / "workspace with spaces 日本語").resolve()
    ctx = SimpleNamespace(root=root, state=root / "_local/state")
    cmd = orch.codex_cmd(ctx, root / "worktree", "codex", "model", "medium", idb,
                         root / "evidence", resume)
    assert "--dangerously-bypass-hook-trust" in cmd
    config = {}
    for index, arg in enumerate(cmd[:-1]):
        if arg == "-c":
            config.update(tomllib.loads(cmd[index + 1]))
    hook = config["hooks"]["PostToolUse"][0]
    assert hook["matcher"] == ".*"
    command = hook["hooks"][0]
    assert command == {"type": "command", "timeout": 10, "command": subprocess.list2cmdline([
        str(root / "_local/venv/tools/Scripts/python.exe"), str(root / "scripts/usage_guard.py"),
        "--provider", "codex"])}
    assert cmd[-2:] == ["--json", "-"]


def test_capacity_reads_codex_and_report(tmp_path, monkeypatch):
    write_json(tmp_path / "usage.json", {"codex": {"five_hour_used_percent": 81,
                                                  "resets_at": 9999999999}})
    assert capacity.read_provider_usage(tmp_path, "codex")["five_hour_used_percent"] == 81
    ctx = SimpleNamespace(state=tmp_path, data=tmp_path, repos=lambda: {}, workers={})
    monkeypatch.setattr(orch, "load_tasks", lambda *_: {})
    result = orch.capacity_reports(ctx)["codex"]
    assert result["usage"] == 81 and result["recommended"] == 0


def test_notify(monkeypatch, capsys):
    monkeypatch.setattr(usage_watch, "collect", lambda *_: {
        "claude": {"five_hour_used_percent": 80}, "codex": {"five_hour_used_percent": 79}})
    monkeypatch.setattr(usage_watch, "threshold", lambda *_: 80)
    assert usage_watch.main(["--notify"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sum("利用枠の通知" in line for line in lines) == 1
    assert "claude" in next(line for line in lines if "利用枠の通知" in line)


def test_claude_hooks_keep_statusline():
    root = Path(__file__).resolve().parents[2]
    settings = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))
    assert "scripts/statusline.py" in settings["statusLine"]["command"]
    for event in ("PreToolUse", "PostToolUse"):
        entry = settings["hooks"][event][0]
        assert entry["matcher"] == ".*"
        assert entry["hooks"][0]["command"] == "${CLAUDE_PROJECT_DIR}/_local/venv/tools/Scripts/python.exe"
        assert entry["hooks"][0]["args"] == [
            "${CLAUDE_PROJECT_DIR}/scripts/usage_guard.py", "--provider", "claude"]


def test_expire_independent_windows():
    original = {"five_hour_used_percent": 85, "resets_at": 100,
                "weekly_used_percent": 90, "weekly_resets_at": 200}
    assert usage_watch.expire(original, 99) == original
    assert usage_watch.expire(original, 100)["five_hour_used_percent"] == 0
    assert usage_watch.expire(original, 100)["weekly_used_percent"] == 90
    assert usage_watch.expire(original, 200)["weekly_used_percent"] == 0
    assert original["five_hour_used_percent"] == 85
