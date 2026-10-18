#!/usr/bin/env python3
"""Claude Code の status line。標準入力の JSON から1行を作り、使用率を _local/state/usage.json へ書く。

JSON の項目（https://code.claude.com/docs/en/statusline）:
model.display_name / workspace.current_dir / rate_limits.five_hour.{used_percentage,resets_at}
rate_limits は claude.ai の Pro・Max 契約で、最初の API 応答の後にだけ現れる。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("AC3DS_WORKSPACE") or Path(__file__).resolve().parent.parent)


def _get(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def write_usage(root: Path, pct, resets_at) -> None:
    from usage_watch import update_usage
    update_usage(root / "_local" / "state", {
        "claude": {"five_hour_used_percent": pct, "resets_at": resets_at, "updated": int(time.time())}})


FIVE_NAMES = ("five_hour", "fiveHour", "5h", "five_hours")
PCT_NAMES = ("used_percentage", "used_percent", "usedPercentage", "percent_used", "utilization")
RESET_NAMES = ("resets_at", "reset_at", "resetsAt")


def _first(d, names):
    if isinstance(d, dict):
        for n in names:
            if d.get(n) is not None:
                return d[n]
    return None


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def find_five_hour(data):
    """(使用率, resets_at) を返す。公式の項目は rate_limits.five_hour.used_percentage。別名・別階層も許す。"""
    rl = data.get("rate_limits") if isinstance(data.get("rate_limits"), dict) else None
    for holder in (rl, data):
        win = _first(holder, FIVE_NAMES)
        if isinstance(win, dict):
            return _num(_first(win, PCT_NAMES)), _first(win, RESET_NAMES)
        if win is not None and _num(win) is not None:  # five_hour が数値そのもの
            return _num(win), None
    if rl is not None:  # five_hour の無い rate_limits に、直に使用率があれば
        return _num(_first(rl, PCT_NAMES)), _first(rl, RESET_NAMES)
    return None, None


def save_last(root: Path, raw: str) -> None:
    try:
        p = root / "_local" / "state" / "statusline_last.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(raw, encoding="utf-8", newline="")
    except Exception:
        pass


def rel_dir(root: Path, cur) -> str:
    if not cur:
        return "?"
    try:
        r = Path(cur).resolve().relative_to(root.resolve())
        return r.as_posix()
    except (ValueError, OSError):
        return str(cur)


def render(raw: str, root: Path = ROOT) -> str:
    save_last(root, raw)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return "status line: 入力が読めない"
    model = _get(data, "model", "display_name") or "?"
    cur = _get(data, "workspace", "current_dir") or data.get("cwd")
    pct, resets = find_five_hour(data)
    if pct is not None:
        usage = "5h %.0f%%" % pct
        try:
            write_usage(root, pct, resets)
        except Exception:
            usage += "（記録失敗）"
    else:
        usage = "5h 使用率: 取得不可"
    return "%s | %s | %s" % (model, rel_dir(root, cur), usage)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        line = render(raw)
    except Exception:  # 必ず1行出す
        line = "status line: エラー"
    print(line.replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
