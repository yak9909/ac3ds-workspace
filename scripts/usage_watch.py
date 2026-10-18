#!/usr/bin/env python3
"""Claude と Codex の利用枠を集計する。モデルは起動しない。"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
import tomllib
from pathlib import Path

from filelock import FileLock

import wslib


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def expire(data, now):
    data = dict(data)
    for key, reset in (("five_hour_used_percent", "resets_at"),
                       ("weekly_used_percent", "weekly_resets_at")):
        deadline = number(data.get(reset))
        if deadline is not None and deadline <= now:
            data[key] = 0
    return data


def update_usage(state, updates):
    """提供元ごとの差分を、statusline と共有するロックの下で保存する。"""
    state.mkdir(parents=True, exist_ok=True)
    path = state / "usage.json"
    with FileLock(str(state / "usage.lock"), timeout=2):
        data = read_json(path)
        data.update(updates(data) if callable(updates) else updates)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state,
                                             suffix=".tmp", delete=False) as f:
                name = f.name
                json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.write("\n")
            os.replace(name, path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
    return data


def reverse_lines(path):
    """大きな rollout も末尾から必要な分だけ読む。"""
    with path.open("rb") as f:
        f.seek(0, 2)
        pos, tail = f.tell(), b""
        while pos:
            size = min(pos, 65536)
            pos -= size
            f.seek(pos)
            lines = (f.read(size) + tail).split(b"\n")
            tail = lines[0]
            yield from reversed(lines[1:])
        yield tail


def codex_usage(sessions, now):
    paths = list(sessions.glob("**/rollout-*.jsonl"))
    if not paths:
        return None
    latest = max(paths, key=lambda p: p.stat().st_mtime_ns)
    for line in reverse_lines(latest):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") if event.get("type") == "event_msg" else event
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        limits = payload.get("rate_limits")
        if not isinstance(limits, dict):
            return None
        data = {"updated": int(now)}
        for name in ("primary", "secondary"):
            window = limits.get(name)
            if not isinstance(window, dict):
                continue
            minutes = number(window.get("window_minutes"))
            if minutes == 300:
                key, reset = "five_hour_used_percent", "resets_at"
            elif minutes == 10080:
                key, reset = "weekly_used_percent", "weekly_resets_at"
            else:
                continue
            data[key] = number(window.get("used_percent"))
            data[reset] = window.get("resets_at")
        return expire(data, now)
    return None


def threshold(root):
    override = number(os.environ.get("AC3DS_GUARD_THRESHOLD"))
    if override is not None:
        return override
    try:
        config = tomllib.loads((root / "workers.toml").read_text(encoding="utf-8"))
        value = number(config.get("limits", {}).get("stop_at_used_percent"))
        return value if value is not None else 80
    except (OSError, ValueError):
        return 80


def collect(root=None, *, sessions=None, now=None):
    root = root or wslib.workspace_root()
    now = time.time() if now is None else now
    state = root / "_local" / "state"
    sessions = sessions or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    try:
        codex = codex_usage(sessions, now)
    except OSError:
        codex = None  # 他方の提供元の集計は続ける

    def updates(data):
        claude = data.get("claude", {})
        claude = claude if isinstance(claude, dict) else {}
        last = state / "statusline_last.json"
        if not claude or (last.exists() and last.stat().st_mtime > (number(claude.get("updated")) or 0)):
            from statusline import find_five_hour
            pct, reset = find_five_hour(read_json(last))
            if pct is not None:
                claude = {"five_hour_used_percent": pct, "resets_at": reset,
                          "updated": int(last.stat().st_mtime)}
        cached = data.get("codex", {})
        current = codex if codex is not None else (cached if isinstance(cached, dict) else {})
        return {"claude": expire(claude, now), "codex": expire(current, now)}

    return update_usage(state, updates)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args(argv)
    root = wslib.workspace_root()
    data = collect(root)
    for provider in ("claude", "codex"):
        pct = number(data.get(provider, {}).get("five_hour_used_percent"))
        print("%s: 5時間枠 %s" % (provider, "不明" if pct is None else "%g%%" % pct))
        if args.notify and pct is not None and pct >= threshold(root):
            print("利用枠の通知: %s の5時間枠が %g%% に達した。" % (provider, pct))
    return 0


if __name__ == "__main__":
    wslib.configure_stdio()
    raise SystemExit(main())
