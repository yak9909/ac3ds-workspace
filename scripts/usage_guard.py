#!/usr/bin/env python3
"""フックから利用枠の中断指示を、セッションごとに一度だけ差し込む。"""
from __future__ import annotations

import hashlib
import json
import re
import sys

import usage_watch
import wslib


def guard(provider, payload, root=None):
    root = root or wslib.workspace_root()
    if provider not in ("claude", "codex") or not isinstance(payload, dict):
        return None
    session = payload.get("session_id")
    event = payload.get("hook_event_name")
    if not isinstance(session, str) or not session or event not in ("PreToolUse", "PostToolUse"):
        return None
    data = usage_watch.collect(root)
    pct = usage_watch.number(data.get(provider, {}).get("five_hour_used_percent"))
    if pct is None or pct < usage_watch.threshold(root):
        return None
    message = "利用枠の5時間枠が %g%% に達した。" % pct
    if provider == "codex" or payload.get("agent_id"):
        message += "区切りの良い所で作業を止め、作業ツリーの .task/notes.md に経過と次の1手を書き、報告して終える。"
    else:
        message += "新しい依頼を出さず、スキル wrapup の手順で中断する。"
    safe = session if re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", session) else hashlib.sha256(session.encode()).hexdigest()
    marker = root / "_local" / "state" / "guard" / (provider + "-" + safe)
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="utf-8") as f:
            f.write(message + "\n")
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
