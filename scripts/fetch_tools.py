#!/usr/bin/env python3
"""scripts/third_party.toml の外部ツールを _local/tools/<名前>/<版>/ へ取得し、SHA256 で照合する。"""
from __future__ import annotations

import hashlib
import shutil
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wslib  # noqa: E402


class FetchError(Exception):
    pass


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as r, open(part, "wb") as f:
        shutil.copyfileobj(r, f)
    part.replace(dest)


def _safe_extract(zpath: Path, dest: Path) -> None:
    base = dest.resolve()
    with zipfile.ZipFile(zpath) as z:
        for m in z.namelist():
            if base not in (base / m).resolve().parents and (base / m).resolve() != base:
                raise FetchError("zip に範囲外のパスがある: %s" % m)
        z.extractall(dest)


def find_exe(vdir: Path, exe: str) -> Path | None:
    p = vdir / exe
    if p.is_file():
        return p
    for c in sorted(vdir.rglob(Path(exe).name)):
        if "download" not in c.relative_to(vdir).parts and c.is_file():
            return c
    return None


def ensure_tool(name: str, spec: dict, tools_dir: Path, fetch=download) -> str:
    """'ok'（既にあって一致）か 'fetched' を返す。不一致は FetchError（何も消さない）。"""
    vdir = tools_dir / name / spec["version"]
    exe = find_exe(vdir, spec["exe"]) if vdir.is_dir() else None
    if exe is not None:
        if sha256_file(exe) != spec["exe_sha256"]:
            raise FetchError("%s: 展開済みの %s の SHA256 が台帳と違う（消さずに止める）" % (name, exe))
        return "ok"
    dl = vdir / "download"
    dl.mkdir(parents=True, exist_ok=True)
    zpath = dl / Path(spec["url"]).name
    if not zpath.exists():
        fetch(spec["url"], zpath)
    if sha256_file(zpath) != spec["sha256"]:
        raise FetchError("%s: zip の SHA256 が台帳と違う（%s を消さずに止める）" % (name, zpath))
    _safe_extract(zpath, vdir)
    exe = find_exe(vdir, spec["exe"])
    if exe is None:
        raise FetchError("%s: 展開後に %s が見つからない" % (name, spec["exe"]))
    if sha256_file(exe) != spec["exe_sha256"]:
        raise FetchError("%s: 展開後の %s の SHA256 が台帳と違う（消さずに止める）" % (name, exe))
    return "fetched"


def main(argv=None) -> int:
    wslib.configure_stdio()
    root = wslib.workspace_root()
    with open(root / "scripts" / "third_party.toml", "rb") as f:
        tools = tomllib.load(f).get("tools", {})
    tools_dir = root / wslib.load_workspace(root).get("data", "_local") / "tools"
    rc = 0
    for name, spec in tools.items():
        try:
            print("%s %s: %s" % (name, spec["version"], ensure_tool(name, spec, tools_dir)))
        except (FetchError, OSError, zipfile.BadZipFile) as e:
            print("エラー: %s" % e, file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
