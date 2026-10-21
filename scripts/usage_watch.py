#!/usr/bin/env python3
"""Claude と Codex の利用枠を集計する。モデルは起動しない。"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass
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


CODEX_SCAN_FILES = 8  # 更新の新しい rollout を、この本数まで見る（並行のワーカーの分も含む）


def window_values(limits):
    """rate_limits の 300 分・10080 分の窓の数値。どちらも数値が無ければ None（null の窓は無いものとして扱う）。"""
    data = {}
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        used = number(window.get("used_percent"))
        if used is None:
            continue
        minutes = number(window.get("window_minutes"))
        if minutes == 300:
            key, reset = "five_hour_used_percent", "resets_at"
        elif minutes == 10080:
            key, reset = "weekly_used_percent", "weekly_resets_at"
        else:
            continue
        data[key] = used
        data[reset] = window.get("resets_at")
    return data or None


def event_epoch(event):
    """イベントの timestamp（ISO 8601）を epoch 秒にする。無い・読めないときは None。"""
    text = event.get("timestamp")
    if not isinstance(text, str):
        return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_codex_limits(path):
    """rollout の末尾から、窓の数値を持つ rate_limits を探す。limit_id が "codex" の物を優先する。
    見つかれば (イベントの時刻または None, 窓の数値)、無ければ None。"""
    fallback = None
    for line in reverse_lines(path):
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
            continue
        values = window_values(limits)
        if values is None:
            continue
        found = (event_epoch(event), values)
        if limits.get("limit_id") == "codex":
            return found
        if fallback is None:
            fallback = found
    return fallback


def file_mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def codex_usage(sessions, now):
    """更新の新しい数本の rollout を見て、一番新しい有効な窓の値を使う。有効な値が無ければ None。"""
    paths = list(sessions.glob("**/rollout-*.jsonl"))
    if not paths:
        return None
    recent = sorted(paths, key=file_mtime, reverse=True)[:CODEX_SCAN_FILES]
    best = None
    for path in recent:
        try:
            found = last_codex_limits(path)
        except OSError:
            continue
        if found is None:
            continue
        stamp, values = found
        key = stamp if stamp is not None else file_mtime(path)
        if best is None or key > best[0]:
            best = (key, values)
    if best is None:
        return None
    return expire({"updated": int(now), **best[1]}, now)


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


GUARD_INTERVAL_SEC = 60  # 見張りの間隔（試験では差し替える）


def five_hour_percent(root, provider):
    """提供元の5時間枠の使用率を、集計し直して返す。取れなければ None（見張りは止めない）。"""
    try:
        return number(collect(root).get(provider, {}).get("five_hour_used_percent"))
    except Exception:  # 集計の失敗で作業を止めない
        return None


@dataclass
class GuardRun:
    returncode: int
    stdout: str
    stderr: str
    stopped: bool  # 使用率の閾値で子プロセスを止めたか
    used_percent: float | None  # 止めたときの使用率


def _drain(stream, chunks):
    try:
        for line in iter(stream.readline, ""):
            chunks.append(line)
    except (OSError, ValueError):
        pass


def _feed(stream, text):
    try:
        if text:
            stream.write(text)
        stream.close()
    except (OSError, ValueError):
        pass


def kill_tree(proc):
    """子プロセスごと止める。Windows は taskkill /T、それ以外はプロセスグループ全体。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_with_guard(cmd, *, input_text="", cwd=None, env=None, root=None, provider="codex",
                   threshold_pct=None, interval=None, usage=None):
    """cmd を起動し、終わるまで待つ。interval 秒ごとに provider の5時間枠の使用率を見て、
    閾値以上になったら子プロセスごと止める。閾値は workers.toml の stop_at_used_percent（既定 80）。
    usage は使用率を返す関数（試験で差し替える。既定は collect で集計する）。戻り値は GuardRun。"""
    root = root or wslib.workspace_root()
    limit = threshold_pct if threshold_pct is not None else threshold(root)
    interval = GUARD_INTERVAL_SEC if interval is None else interval
    read = usage or (lambda: five_hour_percent(root, provider))
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            start_new_session=os.name != "nt")
    out, err = [], []
    threads = [threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True),
               threading.Thread(target=_feed, args=(proc.stdin, input_text), daemon=True)]
    for t in threads:
        t.start()
    stopped, used = False, None
    while True:
        try:
            proc.wait(timeout=interval)
            break
        except subprocess.TimeoutExpired:
            pct = read()
            if pct is not None and pct >= limit:
                stopped, used = True, pct
                kill_tree(proc)
                break
    for t in threads:
        t.join(timeout=10)
    return GuardRun(proc.returncode, "".join(out), "".join(err), stopped, used)


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
