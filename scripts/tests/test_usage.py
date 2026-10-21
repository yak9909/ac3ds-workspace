import io
import json
import os
import subprocess
import sys
import time
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
    assert len(list((tmp_path / "_local/state/guard").glob(provider + "-v2-*.json"))) == 1
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


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_guard_once_per_window(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("AC3DS_GUARD_THRESHOLD", "80")
    clock = [100]
    usage = {provider: {"five_hour_used_percent": 85, "resets_at": 200}}
    monkeypatch.setattr(usage_guard.time, "time", lambda: clock[0])
    monkeypatch.setattr(usage_watch, "collect", lambda *_: usage)
    payload = {"session_id": "continued-session", "hook_event_name": "PreToolUse"}

    assert usage_guard.guard(provider, payload, tmp_path) is not None
    payload["hook_event_name"] = "PostToolUse"
    usage[provider]["five_hour_used_percent"] = 99
    usage[provider]["resets_at"] = "200.0"  # 数値の表現が変わっても同じ枠
    assert usage_guard.guard(provider, payload, tmp_path) is None
    clock[0] = 199
    assert usage_guard.guard(provider, payload, tmp_path) is None

    clock[0] = 200
    usage[provider]["resets_at"] = 400
    assert usage_guard.guard(provider, payload, tmp_path) is not None
    assert usage_guard.guard(provider, payload, tmp_path) is None
    markers = list((tmp_path / "_local/state/guard").glob("*.json"))
    assert len(markers) == 1
    assert json.loads(markers[0].read_text(encoding="utf-8"))["expires_at"] == 400


@pytest.mark.parametrize("agents", [
    [None, "worker-1", "worker-2"], ["worker-2", "worker-1", None]])
def test_guard_each_agent_in_shared_session(tmp_path, monkeypatch, agents):
    monkeypatch.setenv("AC3DS_GUARD_THRESHOLD", "80")
    monkeypatch.setattr(usage_guard.time, "time", lambda: 100)
    monkeypatch.setattr(usage_watch, "collect", lambda *_: {
        "claude": {"five_hour_used_percent": 80, "resets_at": 200}})
    for agent in agents:
        payload = {"session_id": "shared", "agent_id": agent, "hook_event_name": "PreToolUse"}
        output = usage_guard.guard("claude", payload, tmp_path)
        message = output["hookSpecificOutput"]["additionalContext"]
        assert (".task/notes.md" if agent else "スキル wrapup") in message
        assert usage_guard.guard("claude", payload, tmp_path) is None
    assert len(list((tmp_path / "_local/state/guard").glob("*.json"))) == len(agents)


@pytest.mark.parametrize("reset", [None, "invalid"])
def test_guard_without_reset_uses_time_window(tmp_path, monkeypatch, reset):
    monkeypatch.setenv("AC3DS_GUARD_THRESHOLD", "80")
    clock = [100]
    monkeypatch.setattr(usage_guard.time, "time", lambda: clock[0])
    monkeypatch.setattr(usage_watch, "collect", lambda *_: {
        "claude": {"five_hour_used_percent": 85, "resets_at": reset}})
    payload = {"session_id": "shared", "hook_event_name": "PreToolUse"}
    assert usage_guard.guard("claude", payload, tmp_path) is not None
    clock[0] = 5 * 60 * 60 - 1
    assert usage_guard.guard("claude", payload, tmp_path) is None
    clock[0] = 5 * 60 * 60
    assert usage_guard.guard("claude", payload, tmp_path) is not None
    assert usage_guard.guard("claude", payload, tmp_path) is None
    assert len(list((tmp_path / "_local/state/guard").glob("*.json"))) == 1


def test_guard_cleans_expired_markers_below_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("AC3DS_GUARD_THRESHOLD", "80")
    monkeypatch.setattr(usage_guard.time, "time", lambda: 200)
    monkeypatch.setattr(usage_watch, "collect", lambda *_: {
        "claude": {"five_hour_used_percent": 0, "resets_at": 400}})
    directory = tmp_path / "_local/state/guard"
    expired = directory / "claude-v2-expired.json"
    current = directory / "claude-v2-current.json"
    other_provider = directory / "codex-v2-expired.json"
    write_json(expired, {"expires_at": 200})
    write_json(current, {"expires_at": 201})
    write_json(other_provider, {"expires_at": 100})
    legacy = directory / "claude-old-session"
    recent_legacy = directory / "codex-recent-session"
    legacy.write_text("旧形式", encoding="utf-8")
    recent_legacy.write_text("旧形式", encoding="utf-8")
    os.utime(legacy, (200 - 5 * 60 * 60, 200 - 5 * 60 * 60))
    os.utime(recent_legacy, (200, 200))
    unrelated = directory / "unrelated"
    unrelated.write_text("保持", encoding="utf-8")

    payload = {"session_id": "shared", "hook_event_name": "PreToolUse"}
    assert usage_guard.guard("claude", payload, tmp_path) is None
    assert set(directory.iterdir()) == {current, recent_legacy, unrelated}


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


def rate_event(limit_id, primary, secondary, stamp):
    return {"timestamp": stamp, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "limit_id": limit_id, "primary": primary, "secondary": secondary}}}


def write_lines(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")


CODEX_WINDOWS = {"used_percent": 99.0, "window_minutes": 300, "resets_at": 1792564564}
CODEX_WEEK = {"used_percent": 54.0, "window_minutes": 10080, "resets_at": 1793090245}


def test_codex_usage_skips_null_premium_at_end(tmp_path):
    """実際のログの形: 最後の token_count は premium で窓が null。その手前の codex の値を使う。"""
    sessions = tmp_path / "sessions"
    write_lines(sessions / "2026/10/21/rollout-2026-10-21T10-35-52-a.jsonl", [
        {"type": "session_meta", "payload": {}},
        rate_event("codex", CODEX_WINDOWS, CODEX_WEEK, "2026-10-21T01:00:00Z"),
        rate_event("premium", None, None, "2026-10-21T01:05:00Z"),
    ])
    data = usage_watch.collect(tmp_path, sessions=sessions, now=1792000000)
    assert data["codex"]["five_hour_used_percent"] == 99.0
    assert data["codex"]["resets_at"] == 1792564564
    assert data["codex"]["weekly_used_percent"] == 54.0


def test_codex_usage_prefers_codex_limit_and_newest_file(tmp_path):
    sessions = tmp_path / "sessions"
    # 新しい更新時刻のファイルは、窓の無い行しか無い（別のワーカーが書き始めたばかり）
    empty = sessions / "b/rollout-b.jsonl"
    write_lines(empty, [rate_event("premium", None, None, "2026-10-21T02:00:00Z")])
    older = sessions / "a/rollout-a.jsonl"
    write_lines(older, [
        rate_event("codex", CODEX_WINDOWS, None, "2026-10-21T01:00:00Z"),
        rate_event("premium", {"used_percent": 90.0, "window_minutes": 300, "resets_at": 1792564564},
                   None, "2026-10-21T01:30:00Z"),
    ])
    os.utime(older, (10, 10))
    os.utime(empty, (20, 20))
    data = usage_watch.codex_usage(sessions, 1792000000)
    assert data["five_hour_used_percent"] == 99.0  # 後ろの premium より limit_id が codex の物を優先


def test_codex_usage_newest_valid_across_files(tmp_path):
    sessions = tmp_path / "sessions"
    newer_mtime = sessions / "x/rollout-x.jsonl"
    write_lines(newer_mtime, [rate_event("codex", {"used_percent": 10.0, "window_minutes": 300, "resets_at": 1792564564},
                                         None, "2026-10-21T01:00:00Z")])
    older_mtime = sessions / "y/rollout-y.jsonl"
    write_lines(older_mtime, [rate_event("codex", {"used_percent": 50.0, "window_minutes": 300, "resets_at": 1792564564},
                                         None, "2026-10-21T02:00:00Z")])
    os.utime(newer_mtime, (30, 30))
    os.utime(older_mtime, (20, 20))
    assert usage_watch.codex_usage(sessions, 1792000000)["five_hour_used_percent"] == 50.0


def test_codex_usage_none_when_no_window(tmp_path):
    sessions = tmp_path / "sessions"
    write_lines(sessions / "a/rollout-a.jsonl", [rate_event("premium", None, None, "2026-10-21T01:00:00Z")])
    assert usage_watch.codex_usage(sessions, 100) is None
    assert usage_watch.codex_usage(tmp_path / "absent", 100) is None


# ---------- 実行中の見張り ----------
def test_run_with_guard_stops_child_tree_at_threshold(tmp_path):
    marker = tmp_path / "grandchild-alive.txt"
    ready = tmp_path / "ready.txt"
    grand = "import pathlib,sys,time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text('alive')"
    # 子の子を作り終えてから、印を書く。作る途中で止めると、子の子が木の外に残る（試験の前提）
    parent = ("import pathlib,subprocess,sys,time\n"
              "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]])\n"
              "pathlib.Path(sys.argv[3]).write_text('ready')\n"
              "print('開始', flush=True)\ntime.sleep(60)\n")
    reads = []

    def usage():
        reads.append(1)
        return 85 if ready.exists() else 10

    started = time.monotonic()
    r = usage_watch.run_with_guard([sys.executable, "-c", parent, str(marker), grand, str(ready)], root=tmp_path,
                                   threshold_pct=80, interval=0.2, usage=usage)
    assert time.monotonic() - started < 30
    assert r.stopped is True and r.used_percent == 85 and r.returncode != 0
    assert "開始" in r.stdout and reads
    time.sleep(3)  # 子の子が生きていれば、2 秒後に印を書く
    assert not marker.exists()


def test_run_with_guard_below_threshold_runs_to_end(tmp_path):
    cmd = [sys.executable, "-c", "import time; time.sleep(0.6); print('終わり')"]
    reads = []

    def usage():
        reads.append(1)
        return 79.9

    r = usage_watch.run_with_guard(cmd, root=tmp_path, threshold_pct=80, interval=0.1, usage=usage)
    assert r.stopped is False and r.used_percent is None and r.returncode == 0
    assert "終わり" in r.stdout and reads


def test_run_with_guard_unknown_usage_never_stops(tmp_path):
    cmd = [sys.executable, "-c", "import time; time.sleep(0.5)"]
    r = usage_watch.run_with_guard(cmd, root=tmp_path, threshold_pct=80, interval=0.1, usage=lambda: None)
    assert r.stopped is False and r.returncode == 0


def test_run_with_guard_feeds_stdin(tmp_path):
    cmd = [sys.executable, "-c", "import sys; print('受けた: ' + sys.stdin.read())"]
    r = usage_watch.run_with_guard(cmd, input_text="依頼文", root=tmp_path, usage=lambda: None)
    assert r.returncode == 0 and "受けた: 依頼文" in r.stdout


def test_five_hour_percent_failure_is_none(tmp_path, monkeypatch):
    def fail(*_, **__):
        raise PermissionError("読めない")
    monkeypatch.setattr(usage_watch, "collect", fail)
    assert usage_watch.five_hour_percent(tmp_path, "codex") is None
