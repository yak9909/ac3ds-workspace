"""作業場の道具が共有する小さな関数（設定の読み込み・ac3ds-re の audit.py の読み込み）。"""
from __future__ import annotations

import importlib.util
import os
import sys
import tomllib
from pathlib import Path


def workspace_root() -> Path:
    env = os.environ.get("AC3DS_WORKSPACE")
    return Path(env).resolve() if env else Path(__file__).resolve().parent.parent


def _load(p: Path) -> dict:
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


def load_workspace(root: Path) -> dict:
    return _load(root / "workspace.toml")


def load_local(root: Path) -> dict:
    return _load(root / "workspace.local.toml")


def git_exe(root: Path) -> str:
    return os.environ.get("AC3DS_GIT") or load_local(root).get("git") or "git"


def existing_repos(root: Path) -> dict[str, dict]:
    """workspace.toml の repos のうち、実際にフォルダがある物。"""
    return {n: r for n, r in load_workspace(root).get("repos", {}).items() if (root / n).is_dir()}


def configure_stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def load_re_audit(root: Path):
    """ac3ds-re/tools/audit.py を読み込む。無ければ RuntimeError。"""
    p = root / "ac3ds-re" / "tools" / "audit.py"
    if not p.exists():
        raise RuntimeError("ac3ds-re が要ります（%s が無い）" % p)
    spec = importlib.util.spec_from_file_location("ac3ds_re_audit", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ac3ds_re_audit"] = mod  # dataclass が参照する
    spec.loader.exec_module(mod)
    return mod
