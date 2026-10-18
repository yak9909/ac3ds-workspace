"""消化候補の検出と Issue 作成。外部コマンドは実行しない。"""
import json
from types import SimpleNamespace

import pytest

import orch


def record(root, scope, category, number, *, state="active", consolidated_into=""):
    folder = root / "ac3ds-re" / "findings" / scope / "0000-0099"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"F{number:04}.md").write_text(
        f"---\ncategory: {category}\nstate: {state}\n"
        f"consolidated_into: '{consolidated_into}'\n---\n本文\n", encoding="utf-8")


def context(tmp_path, monkeypatch, threshold=None):
    root = tmp_path / "workspace"
    (root / "ac3ds-re" / "findings").mkdir(parents=True)
    limits = {} if threshold is None else {"consolidate_threshold": threshold}
    c = SimpleNamespace(root=root, workers={"limits": limits},
                        ws={"repos": {"ac3ds-re": {"remote": "https://github.com/o/ac3ds-re.git"}}},
                        repos=lambda: {"ac3ds-re": {}})
    monkeypatch.setattr(orch, "Ctx", lambda: c)
    return root, c


def test_counts_filter_states_and_consolidated_into(tmp_path, monkeypatch):
    root, c = context(tmp_path, monkeypatch)
    for number in range(1, 12):
        record(root, "acnl", "ui", number)
    record(root, "acnl", "ui", 12, state="consolidated")
    record(root, "acnl", "ui", 13, state="superseded")
    record(root, "acnl", "ui", 14, consolidated_into="topics/acnl/ui.md")
    record(root, "acnl", "_triage", 15)
    record(root, "acnl", "_product", 16)
    record(root, "hhd", "ui", 17)
    assert orch.unconsolidated_counts(root / "ac3ds-re") == {("acnl", "ui"): 11, ("hhd", "ui"): 1}
    assert orch.consolidation_candidates(c, "ac3ds-re") == [("acnl", "ui", 11)]
    assert orch.consolidation_candidates(c, "ac3ds-re", 11) == []
    assert orch.consolidation_candidates(c, "ac3ds-re", 1) == [("acnl", "ui", 11)]


def test_configured_threshold_and_invalid_threshold(tmp_path, monkeypatch):
    root, c = context(tmp_path, monkeypatch, threshold=2)
    for number in range(1, 4):
        record(root, "3ds", "sdk", number)
    assert orch.consolidation_candidates(c, "ac3ds-re") == [("3ds", "sdk", 3)]
    assert orch.consolidation_candidates(c, "ac3ds-re", 3) == []
    assert orch.main(["consolidate-check", "--threshold", "-1"]) == 1


def test_create_issue_body_and_skip_open_issue(tmp_path, monkeypatch, capsys):
    root, _ = context(tmp_path, monkeypatch)
    for number in range(1, 12):
        record(root, "acnl", "ui", number)
        record(root, "hhd", "system", number)
    calls = []
    open_issues = [{"number": 42, "title": "[ACNL] ui の記録を topics にまとめる",
                    "labels": [{"name": "acnl"}, {"name": "consolidate"}]}]

    def fake_gh(args, input_text=None):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps(open_issues)
        if args[:2] == ["issue", "create"]:
            return "https://github.com/o/ac3ds-re/issues/43\n"
        raise AssertionError(args)

    monkeypatch.setattr(orch, "gh_run", fake_gh)
    assert orch.main(["consolidate-check"]) == 0
    assert calls == []
    assert "acnl/ui: 未消化 11 件" in capsys.readouterr().out
    assert orch.main(["consolidate-check", "--create"]) == 0
    created = [args for args in calls if args[:2] == ["issue", "create"]]
    assert len(created) == 1
    args = created[0]
    assert args[args.index("--title") + 1] == "[HHD] system の記録を topics にまとめる"
    assert [args[i + 1] for i, arg in enumerate(args) if arg == "--label"] == ["hhd", "consolidate", "normal"]
    body = args[args.index("--body") + 1]
    assert "### スコープ\n\nhhd" in body
    assert "### 種類\n\nconsolidate" in body
    assert "### 難易度\n\nnormal" in body
    assert "### 目的と完了条件" in body
    assert "topics/hhd/system.md" in body
    assert "格付け A 以上" in body
    assert "利用枠は3割まで" in body
    assert "統合・重複する記録は報告で提案する" in body
    assert "state と consolidated_into は仕上げで確定する" in body
    assert "### 対象\n\nfindings/hhd の system カテゴリ" in body
    assert "consolidate-category" in body
    assert "既存の Issue #42" in capsys.readouterr().out


def test_open_issue_can_be_matched_without_the_expected_title_or_label():
    assert orch.same_consolidation_issue(
        {"title": "[ACNL] ui の記録を topics にまとめる", "labels": []}, "acnl", "ui")
    body = orch.issue_body("acnl", "consolidate", "normal", "目的", "findings/acnl の ui カテゴリ", "資料")
    assert orch.same_consolidation_issue(
        {"title": "[ACNL] UI の消化", "labels": [{"name": "consolidate"}], "body": body}, "acnl", "ui")
    assert not orch.same_consolidation_issue(
        {"title": "[ACNL] UI の消化", "labels": [{"name": "consolidate"}], "body": body}, "hhd", "ui")


@pytest.mark.parametrize("missing", ["repo", "findings", "records"])
def test_automatic_check_skips_missing_records_silently(tmp_path, monkeypatch, capsys, missing):
    root, c = context(tmp_path, monkeypatch)
    if missing != "records":
        (root / "ac3ds-re" / "findings").rmdir()
    if missing == "repo":
        (root / "ac3ds-re").rmdir()
        c.repos = lambda: {}

    # start と accept が共有する呼び出しは、見出しや警告も出さない。
    assert orch.report_consolidation(c, "ac3ds-re", skip_missing=True) == []
    assert capsys.readouterr().out == ""

    c.data = root / "_local"
    c.state = c.data / "state"
    monkeypatch.setattr(orch, "gh_run", lambda *_: "[]")
    monkeypatch.setattr(orch, "mirror", lambda _: None)
    monkeypatch.setattr(orch, "export_builds", lambda _: [])
    assert orch.cmd_start(None) == 0
    output = capsys.readouterr().out
    assert "記録の消化" not in output
    assert "しきい値" not in output

    # 単独のコマンドでは、リポジトリや findings/ の欠落を引き続きエラーにする。
    expected = 0 if missing == "records" else 1
    assert orch.main(["consolidate-check"]) == expected
    error = capsys.readouterr().err
    if missing == "repo":
        assert "リポジトリが無い" in error
    elif missing == "findings":
        assert "findings/ が無い" in error
