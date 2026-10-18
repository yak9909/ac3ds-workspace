import datetime as dt
import json
from types import SimpleNamespace

import capacity
import orch


NOW = dt.datetime(2026, 10, 18, tzinfo=dt.timezone.utc)
WORKERS = {"sonnet": {"provider": "claude"}, "gpt": {"provider": "codex"}}
ISSUES = [{"repo": "ac3ds-re", "number": n, "body": "0x%X" % (n * 16), "kind": "analysis"}
          for n in range(1, 11)]


def test_capacity_usage_unknown_and_limit():
    limits = {"default_hourly_percent": 10}
    known = capacity.calculate("claude", ISSUES, {}, WORKERS, limits, 20, "2026-10-18T02:00:00Z", NOW)
    assert known["headroom"] == 50 and known["cost"] == 20 and known["recommended"] == 2
    unknown = capacity.calculate("codex", ISSUES, {}, WORKERS, limits, None, None, NOW)
    assert unknown["recommended"] == 1
    blocked = capacity.calculate("codex", ISSUES, {}, WORKERS, limits, None, None, NOW,
                                 NOW + dt.timedelta(hours=1))
    assert blocked["recommended"] == 0


def test_overlaps_and_legacy_share():
    issues = [dict(i) for i in ISSUES]
    issues[1]["body"] = "対象 0x10 と 0X20"
    for i in range(7):
        issues[i]["kind"] = "reanalyze"
    selected, overlaps = capacity.candidates(issues, {}, 0.3)
    assert len([i for i in selected if i["kind"] == "reanalyze"]) <= 3
    assert overlaps[0]["number"] == 2 and overlaps[0]["addresses"] == [16]


def test_candidates_exclude_report_blocked_and_needs_user():
    issues = [dict(i) for i in ISSUES[:5]]
    for issue, label in zip(issues[:3], ("report", "blocked", "needs-user")):
        issue["labels"] = [{"name": label}]
    issues[3]["labels"] = ["analysis"]
    selected, overlaps = capacity.candidates(issues, {})
    assert [i["number"] for i in selected] == [4, 5]
    assert overlaps == []
    result = capacity.calculate("codex", issues, {}, WORKERS, {}, None, None, NOW)
    assert [i["number"] for i in result["candidates"]] == [4, 5]


def test_hourly_rate_and_log_limit(tmp_path):
    tasks = {"one": {"worker": "sonnet", "usage_before": 10, "usage_after": 20,
                     "dispatched_at": NOW.isoformat(), "accepted_at": (NOW + dt.timedelta(hours=2)).isoformat()}}
    assert capacity.hourly_rates(tasks, {"sonnet": 10})["sonnet"] == 5
    log = tmp_path / "codex_one.jsonl"
    log.write_text(json.dumps({"error": "usage limit resets at 2026-10-18T03:00:00Z"}), encoding="utf-8")
    assert capacity.codex_limit_until(tmp_path, NOW) == NOW + dt.timedelta(hours=3)


def test_stats_rows():
    rows = orch.stats_rows({
        "one": {"worker": "sonnet", "state": "accepted", "attempts": 2, "git_violation": "HEAD",
                "dispatched_at": NOW.isoformat(), "accepted_at": (NOW + dt.timedelta(hours=2)).isoformat()},
        "two": {"worker": "sonnet", "state": "rejected", "attempts": 1,
                "dispatched_at": NOW.isoformat(), "rejected_at": (NOW + dt.timedelta(hours=4)).isoformat()}})
    row = rows["sonnet"]
    assert (row["count"], row["accepted"], row["rejected"], row["attempts"]) == (2, 1, 1, 3)
    assert row["violations"] == {"git_violation": 1}
    assert sum(row["duration_hours"]) / len(row["duration_hours"]) == 3


def test_dispatch_rejects_capacity_before_work(monkeypatch, tmp_path):
    ctx = SimpleNamespace(workers={"workers": {"gpt": {"provider": "codex"}}, "limits": {}},
                          state=tmp_path, root=tmp_path)
    monkeypatch.setattr(orch, "Ctx", lambda: ctx)
    monkeypatch.setattr(orch, "need_repo", lambda *_: {"role": "analysis"})
    monkeypatch.setattr(orch, "repo_slug", lambda *_: "o/ac3ds-re")
    monkeypatch.setattr(orch, "gh_run", lambda *_: "{}")
    monkeypatch.setattr(orch, "read_issue", lambda *_: {"state": "OPEN", "body": "0x100", "title": "x"})
    monkeypatch.setattr(orch, "load_tasks", lambda *_: {})
    monkeypatch.setattr(orch, "capacity_reports", lambda *_: {"codex": {"recommended": 0}})
    a = SimpleNamespace(repo="ac3ds-re", issue=5, worker="gpt", ignore_limit=False)
    try:
        orch.cmd_dispatch(a)
    except orch.OrchError as e:
        assert "推奨並列数 0" in str(e)
    else:
        raise AssertionError("dispatch が利用枠を無視した")
