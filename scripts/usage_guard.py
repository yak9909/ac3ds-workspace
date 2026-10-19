#!/usr/bin/env python3
"""フックから利用枠の中断指示を、相手・5時間枠ごとに一度だけ差し込む。"""
from __future__ import annotations

import hashlib
import json
import sys
import time

import usage_watch
import wslib


def cleanup_markers(directory, now):
    """期限切れの印を片付ける。同時に別のフックが消しても続ける。"""
    for provider in ("claude", "codex"):
        for marker in directory.glob(provider + "-*"):
            try:
                if marker.name.startswith(provider + "-v2-") and marker.suffix == ".json":
                    expires = usage_watch.number(usage_watch.read_json(marker).get("expires_at"))
                else:
                    # 旧形式には期限が無いので、作成後5時間を過ぎた物だけ消す。
                    expires = marker.stat().st_mtime + 5 * 60 * 60
                if expires is not None and expires <= now:
                    marker.unlink()
            except OSError:
                continue


def guard(provider, payload, root=None):
    root = root or wslib.workspace_root()
    if provider not in ("claude", "codex") or not isinstance(payload, dict):
        return None
    session = payload.get("session_id")
    event = payload.get("hook_event_name")
    if not isinstance(session, str) or not session or event not in ("PreToolUse", "PostToolUse"):
        return None
    data = usage_watch.collect(root)
    now = time.time()
    directory = root / "_local" / "state" / "guard"
    cleanup_markers(directory, now)
    usage = data.get(provider, {})
    pct = usage_watch.number(usage.get("five_hour_used_percent"))
    if pct is None or pct < usage_watch.threshold(root):
        return None
    message = "利用枠の5時間枠が %g%% に達した。" % pct
    if provider == "codex" or payload.get("agent_id"):
        message += "区切りの良い所で作業を止め、作業ツリーの .task/notes.md に経過と次の1手を書き、報告して終える。"
    else:
        message += "新しい依頼を出さず、スキル wrapup の手順で中断する。"
    reset = usage_watch.number(usage.get("resets_at"))
    if reset is None:
        # リセット時刻が無い場合は、UTCの時刻を5時間単位に区切る。
        reset = (now // (5 * 60 * 60) + 1) * (5 * 60 * 60)
    identity = json.dumps([session, payload.get("agent_id") or None, reset], ensure_ascii=False)
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    marker = directory / (provider + "-v2-" + key + ".json")
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="utf-8") as f:
            json.dump({"expires_at": reset, "message": message}, f, ensure_ascii=False)
            f.write("\n")
    except FileExistsError:
        return None
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


def main(argv=None):
    try:
        args = sys.argv[1:] if argv is None else argv
        if len(args) != 2 or args[0] != "--provider":
            return 0
        payload = json.loads(sys.stdin.read())
        result = guard(args[1], payload)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    wslib.configure_stdio()
    sys.exit(main())
