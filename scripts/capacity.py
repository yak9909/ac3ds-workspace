#!/usr/bin/env python3
"""利用枠と open Issue から、提供元ごとの依頼可能数を見積もる。"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path

ACTIVE = {"dispatched", "saved", "stamped"}
LEGACY = {"reanalyze", "consolidate"}
EXCLUDED_LABELS = {"report", "blocked", "needs-user"}
ADDRESS = re.compile(r"(?<![\w])0x[0-9a-fA-F]+\b")
LIMIT_ERROR = re.compile(r"usage limit|rate limit|利用上限|使用上限", re.I)
RESET = re.compile(r"(?:resets?\s*(?:at|in)?|reset_at|リセット(?:時刻)?)[^\d]{0,25}(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:?\d\d)?)", re.I)
ENGLISH_RESET = re.compile(
    r"\b(?:try again at|resets? at)\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)\b", re.I)
MONTHS = {name: month for month, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def instant(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        except ValueError:
            return None
    return None


def addresses(body):
    return {int(m.group(), 16) for m in ADDRESS.finditer(body or "")}


def candidates(issues, tasks, share=0.3, max_parallel=3):
    """稼働中と番地が重なる Issue を除き、旧記録の枠を抑える。"""
    running = {(t.get("repo"), str(t.get("issue"))) for t in tasks.values() if t.get("state") in ACTIVE}
    used = {}
    for key, t in tasks.items():
        if t.get("state") in ACTIVE:
            for addr in addresses(t.get("body", "") or t.get("target", "")):
                used.setdefault(addr, key)
    independent, overlaps = [], []
    for issue in issues:
        labels = {x.get("name") if isinstance(x, dict) else x for x in issue.get("labels", [])}
        if labels & EXCLUDED_LABELS:
            continue
        if (issue["repo"], str(issue["number"])) in running:
            continue
        addr = addresses(issue.get("body", ""))
        shared = set(used) & addr
        if shared:
            overlaps.append({"repo": issue["repo"], "number": issue["number"], "addresses": sorted(shared),
                             "with": sorted(set(used[n] for n in shared))})
            continue
        kind = issue.get("kind") or issue.get("labels", [])
        if isinstance(kind, list):
            kind = next((x.get("name") if isinstance(x, dict) else x for x in kind
                         if (x.get("name") if isinstance(x, dict) else x) in LEGACY), "")
        independent.append((issue, kind))
        for n in addr:
            used[n] = "%s#%s" % (issue["repo"], issue["number"])
    ordinary_count = sum(kind not in LEGACY for _, kind in independent)
    legacy_cap = max(0, math.floor(ordinary_count * share / (1 - share))) if 0 <= share < 1 else len(independent)
    selected = []
    for issue, kind in independent:
        if kind in LEGACY:
            if legacy_cap == 0:
                continue
            legacy_cap -= 1
        selected.append(issue)
    return selected, overlaps


def hourly_rates(tasks, defaults):
    samples = {}
    for t in tasks.values():
        model = t.get("worker")
        before, after = t.get("usage_before"), t.get("usage_after")
        start = instant(t.get("dispatched_at"))
        end = instant(t.get("accepted_at") or t.get("rejected_at"))
        if (not model or not isinstance(before, (int, float)) or isinstance(before, bool)
                or not isinstance(after, (int, float)) or isinstance(after, bool) or not start or not end):
            continue
        hours = (end - start).total_seconds() / 3600
        if hours > 0 and after > before:
            samples.setdefault(model, []).append((after - before) / hours)
    return {m: sum(samples[m]) / len(samples[m]) if samples.get(m) else float(v)
            for m, v in defaults.items()}


def codex_limit_until(log_dir, now):
    if not log_dir.is_dir():
        return None
    for path in sorted(log_dir.glob("codex_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
        fallback = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc) + dt.timedelta(hours=5)
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") not in ("error", "turn.failed"):
                continue
            message = event.get("message")
            if message is None and isinstance(event.get("error"), dict):
                message = event["error"].get("message")
            if not isinstance(message, str) or not LIMIT_ERROR.search(message):
                continue
            iso = RESET.search(message)
            reset = instant(iso.group(1)) if iso else None
            if reset is None and (english := ENGLISH_RESET.search(message)):
                month, day, year, hour, minute, meridiem = english.groups()
                try:
                    local = dt.datetime(int(year), MONTHS[month[:3].lower()], int(day),
                                        int(hour) % 12 + (12 if meridiem.upper() == "PM" else 0), int(minute))
                    reset = local.astimezone(dt.timezone.utc)
                except ValueError:
                    pass
            until = reset or fallback
            if now < until:
                return until
    return None


def calculate(provider, issues, tasks, workers, limits, usage, reset_at, now, limit_until=None):
    max_parallel = min(3, int(limits.get("max_parallel", 3)))
    selected, overlaps = candidates(issues, tasks, float(limits.get("legacy_work_share", 0.3)), max_parallel)
    reserve = 10 if provider == "claude" else 0
    headroom = None if usage is None else max(0.0, float(limits.get("stop_at_used_percent", 80)) - float(usage) - reserve)
    reset = instant(reset_at)
    hours = max(0.0, (reset - now).total_seconds() / 3600) if reset else 5.0
    defaults = {name: float(limits.get("hourly_percent", {}).get(name, limits.get("default_hourly_percent", 10)))
                for name, w in workers.items()
                if w.get("provider") == provider}
    rates = hourly_rates(tasks, defaults)
    costs = {name: rate * hours for name, rate in rates.items()}
    cost = min(costs.values()) if costs else float(limits.get("default_hourly_percent", 10)) * hours
    if limit_until and now < limit_until:
        recommended = 0
    elif headroom is None:
        recommended = min(1, max_parallel, len(selected))
    else:
        recommended = min(max_parallel, len(selected), math.floor(headroom / cost) if cost > 0 else max_parallel)
    return {"provider": provider, "usage": usage, "headroom": headroom, "cost": cost,
            "model_costs": costs, "hours_to_reset": hours, "recommended": recommended,
            "candidates": selected, "overlaps": overlaps, "limit_until": limit_until}


def read_provider_usage(state, provider):
    try:
        from usage_watch import expire
        data = json.loads((state / "usage.json").read_text(encoding="utf-8")).get(provider, {})
        return expire(data, dt.datetime.now(dt.timezone.utc).timestamp()) if isinstance(data, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def read_claude_usage(state):
    return read_provider_usage(state, "claude")


if __name__ == "__main__":
    import sys
    from orch import main

    sys.exit(main(["capacity", *sys.argv[1:]]))
