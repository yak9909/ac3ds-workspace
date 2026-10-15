#!/usr/bin/env python3
"""作業場の公開前の検査。ac3ds-re/tools/audit.py の scan_text を再利用する。

モード: --staged（ステージした物）/ --ci（追跡している物）/ 既定（追跡 + 未追跡で無視されていない物）。
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_skills  # noqa: E402
import wslib  # noqa: E402

# .gitignore のホワイトリストと同じ一覧
ALLOWED_TOP = [".gitattributes", ".github/", ".claude/agents/", ".claude/settings.json", "README.md",
               "AGENTS.md", "CLAUDE.md", "LICENSE", "workspace.toml", "workers.toml", "skills/",
               "templates/", "scripts/"]
ALWAYS_TOP = {".gitignore", ".git"}
# 道具が作る・出る物（Git の管理外）。散乱とは見なさない
GENERATED_TOP = {".agents", ".pytest_cache", "__pycache__"}
MAX_SIZE = 1024 * 1024
TEXT_EXT = {".md", ".toml", ".py", ".yml", ".yaml", ".json", ".txt", ".ps1", ".csv", ".h"}


def check_outgoing(text: str) -> list:
    """外へ出す本文（GitHub への書き込みなど）の検査。個人の情報と秘密だけを見る。Problem のリスト。"""
    mod = wslib.load_re_audit(wslib.workspace_root())
    return mod.scan_text("<outgoing>", text, [])


def allowed_path(path: str) -> bool:
    p = path.replace("\\", "/")
    if p in ALWAYS_TOP:
        return True
    for a in ALLOWED_TOP:
        if a.endswith("/"):
            if p.startswith(a):
                return True
        elif p == a:
            return True
    return False


def scan_file(mod, path: str, data: bytes) -> list:
    path = path.replace("\\", "/")
    out: list = []

    def add(reason: str, ln: int = 0) -> None:
        out.append(mod.Problem(path, ln, reason, path))

    ext = PurePosixPath(path).suffix.lower()
    if not allowed_path(path):
        add("許可された場所の外のファイル")
    if ext in mod.FORBIDDEN_EXT:
        add("禁止ファイル（拡張子 %s）" % ext)
        return out
    if len(data) > MAX_SIZE:
        add("1MB を超えるファイル（%d バイト）" % len(data))
    if path == "AGENTS.md" and len(data) > 16 * 1024:
        add("AGENTS.md が 16KB を超えている")
    if fnmatch.fnmatchcase(path, "skills/*/SKILL.md") and len(data) > 8 * 1024:
        add("SKILL.md が 8KB を超えている")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if ext in TEXT_EXT or ext == "":
            add("UTF-8 として読めない")
        return out
    if ext in TEXT_EXT or ext == "":
        if text.startswith("﻿"):
            add("BOM がある")
        if "\r\n" in text:
            add("CRLF がある", text[:text.index("\r\n")].count("\n") + 1)
    out += mod.scan_text(path, text, [])
    return out


def scatter_warnings(root: Path) -> list[str]:
    """作業場の直下にある、許可されていない物。"""
    ws = wslib.load_workspace(root)
    ok = set(ALWAYS_TOP) | GENERATED_TOP | {"_local", ws.get("data", "_local")}
    ok |= {a.strip("/").split("/")[0] for a in ALLOWED_TOP}
    ok |= set(ws.get("local_only", {}).get("files", []))
    ok |= set(ws.get("repos", {}).keys())
    return ["散乱: 作業場の直下に許可されていない物がある: %s" % p.name
            for p in sorted(root.iterdir()) if p.name not in ok]


def _git(root: Path, args: list[str]) -> bytes:
    r = subprocess.run([wslib.git_exe(root), "-c", "core.quotepath=off", *args], cwd=root, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace"))
    return r.stdout


def list_files(root: Path, mode: str) -> list[tuple[str, bytes]]:
    def names(args):
        return [n for n in _git(root, args).decode("utf-8").split("\0") if n]

    if mode == "staged":
        return [(n, _git(root, ["show", ":" + n]))
                for n in names(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"])]
    tracked = set(names(["ls-files", "-z"]))
    allnames = set(tracked)
    if mode == "default":
        allnames |= set(names(["ls-files", "--others", "--exclude-standard", "-z"]))
    out = []
    for n in sorted(allnames):
        p = root / n
        if n in tracked:
            try:
                out.append((n, _git(root, ["show", ":" + n])))
            except RuntimeError:
                if p.is_file():
                    out.append((n, p.read_bytes()))
        elif p.is_file():
            out.append((n, p.read_bytes()))
    return out


def main(argv=None) -> int:
    wslib.configure_stdio()
    ap = argparse.ArgumentParser(prog="audit.py")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true")
    g.add_argument("--ci", action="store_true")
    a = ap.parse_args(argv)
    mode = "staged" if a.staged else "ci" if a.ci else "default"
    root = wslib.workspace_root()
    try:
        mod = wslib.load_re_audit(root)
    except RuntimeError as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 2
    problems = []
    files = list_files(root, mode)
    for n, data in files:
        problems += scan_file(mod, n, data)
    for p in problems:
        print(p)
    if mode != "ci":
        for w in scatter_warnings(root):
            print("警告: " + w)
        for m in sync_skills.check(root):
            problems.append(m)
            print("スキルの写し: " + m)
    print("audit: %d ファイル、問題 %d 件" % (len(files), len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
