#!/usr/bin/env python3
"""スキルの本体（<場所>/skills/）を、各ツールの読み込み場所へコピーする。

- Claude Code: <場所>/.claude/skills/<名前>/
- Codex CLI:   <場所>/.agents/skills/<名前>/   （公式: https://developers.openai.com/codex/skills）
<場所> は作業場と、実際にある各リポジトリ。コピー先は Git に入らない（.gitignore で無視）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wslib  # noqa: E402

TARGET_DIRS = (".claude/skills", ".agents/skills")


def locations(root: Path) -> list[Path]:
    return [root] + [root / n for n in wslib.existing_repos(root)]


def _files(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def source_skills(loc: Path) -> dict[str, Path]:
    s = loc / "skills"
    if not s.is_dir():
        return {}
    return {d.name: d for d in sorted(s.iterdir()) if d.is_dir() and (d / "SKILL.md").exists()}


def check_location(loc: Path) -> list[str]:
    """本体とコピー先の食い違いを文字列で返す。"""
    src = source_skills(loc)
    if not src:
        return []
    out: list[str] = []
    for t in TARGET_DIRS:
        tdir = loc / t
        for name, d in src.items():
            td = tdir / name
            if not td.is_dir():
                out.append("%s: 写しが無い" % td)
            elif _files(d) != _files(td):
                out.append("%s: 本体と一致しない" % td)
        if tdir.is_dir():
            for d in sorted(tdir.iterdir()):
                if d.is_dir() and d.name not in src:
                    out.append("%s: 本体に無いスキル" % d)
    return out


def check(root: Path) -> list[str]:
    out: list[str] = []
    for loc in locations(root):
        out += check_location(loc)
    return out


def sync_location(loc: Path) -> list[str]:
    src = source_skills(loc)
    log: list[str] = []
    for t in TARGET_DIRS:
        tdir = loc / t
        if not src and not tdir.is_dir():
            continue
        tdir.mkdir(parents=True, exist_ok=True)
        for name, d in src.items():
            td = tdir / name
            if td.is_dir() and _files(d) == _files(td):
                continue
            if td.exists():
                shutil.rmtree(td)
            shutil.copytree(d, td)
            log.append("コピー: %s" % td)
        for d in sorted(tdir.iterdir()):
            if d.is_dir() and d.name not in src:
                shutil.rmtree(d)
                log.append("削除: %s" % d)
    return log


def sync(root: Path) -> list[str]:
    log: list[str] = []
    for loc in locations(root):
        log += sync_location(loc)
    return log


def main(argv=None) -> int:
    wslib.configure_stdio()
    ap = argparse.ArgumentParser(prog="sync_skills.py")
    ap.add_argument("--check", action="store_true", help="一致の検査だけをする")
    a = ap.parse_args(argv)
    root = wslib.workspace_root()
    if a.check:
        bad = check(root)
        for b in bad:
            print(b)
        print("sync_skills: 食い違い %d 件" % len(bad))
        return 1 if bad else 0
    log = sync(root)
    for l in log:
        print(l)
    print("sync_skills: 変更 %d 件" % len(log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
