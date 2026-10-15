#!/usr/bin/env python3
"""派生リポジトリのコミット前検査。解析リポジトリへの参照と、個人の情報・秘密を拒否する。

派生リポジトリの中で `python <作業場>/scripts/check_derived.py --staged` として呼ぶ。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wslib  # noqa: E402

RULES = [
    (re.compile(r"\b(?:ACNL|HHD|3DS)-F\d{4,}\b"), "解析の記録 ID"),
    (re.compile(r"ac3ds-re"), "解析リポジトリの名前"),
    (re.compile(r"(?<![\w-])(?:findings|topics)/"), "解析リポジトリへのパス"),
]


def check_text(path: str, text: str, audit_mod=None) -> list[str]:
    out: list[str] = []
    for ln, line in enumerate(text.split("\n"), 1):
        for rx, label in RULES:
            if rx.search(line):
                out.append("%s:%d: %s" % (path, ln, label))
    if audit_mod is not None:
        out += [str(p) for p in audit_mod.scan_text(path, text, [])]
    return out


def _git(args: list[str]) -> bytes:
    exe = os.environ.get("AC3DS_GIT") or wslib.load_local(wslib.workspace_root()).get("git") or "git"
    r = subprocess.run([exe, "-c", "core.quotepath=off", *args], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace"))
    return r.stdout


def main(argv=None) -> int:
    wslib.configure_stdio()
    ap = argparse.ArgumentParser(prog="check_derived.py")
    ap.add_argument("--staged", action="store_true", required=True)
    ap.parse_args(argv)
    try:
        mod = wslib.load_re_audit(wslib.workspace_root())
    except RuntimeError as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 2
    names = [n for n in _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"])
             .decode("utf-8").split("\0") if n]
    problems: list[str] = []
    for n in names:
        data = _git(["show", ":" + n])
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # バイナリは内容を見ない
        problems += check_text(n, text, mod)
    for p in problems:
        print(p)
    print("check_derived: %d ファイル、問題 %d 件" % (len(names), len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
