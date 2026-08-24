#!/usr/bin/env python3
"""Pre-deploy guard: refuse container recreation while analysis work is active.

Usage (on the Ubuntu host):
    python3 scripts/deploy_guard.py [--api http://127.0.0.1:8000] [--force]

Checks the running stock-server for queued/processing analysis tasks.
Exit codes:
    0 = idle (safe to rebuild/recreate)
    1 = busy (tasks in flight; wait or pass --force to override)
    2 = API unreachable (cannot judge; inspect manually)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def fetch_counts(api_base: str) -> dict:
    url = f"{api_base.rstrip('/')}/api/v1/analysis/tasks?limit=1"
    with urllib.request.urlopen(url, timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return {
        "pending": int(payload.get("pending") or 0),
        "processing": int(payload.get("processing") or 0),
        "total": int(payload.get("total") or 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--force", action="store_true", help="report but do not fail on busy")
    args = parser.parse_args()

    try:
        counts = fetch_counts(args.api)
    except Exception as exc:
        print(f"[deploy-guard] 无法访问 {args.api}: {exc}")
        if args.force:
            print("[deploy-guard] --force 指定：忽略不可判定状态，继续部署。")
            return 0
        print("[deploy-guard] 状态未知，拒绝继续；人工确认后加 --force。")
        return 2

    busy = counts["pending"] + counts["processing"]
    state = "BUSY" if busy else "IDLE"
    print(f"[deploy-guard] {state}: pending={counts['pending']} processing={counts['processing']}")
    if busy and not args.force:
        print("[deploy-guard] 存在进行中的分析任务，禁止重建容器；等待完成或使用 --force。")
        return 1
    if busy and args.force:
        print("[deploy-guard] --force 指定：明知有任务在跑仍然继续（任务将被标记失败）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
