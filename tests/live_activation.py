# Copyright 2026 JIALE LIU
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Live measurement: is the new document active when create_new_document returns?

SOLIDWORKS 2016 was seen to report the previous document as active for one to
two seconds after NewDocument.  This script repeats create_new_document under
three conditions and records, per round, what the handler answered, whether
get_active_document_info named the new document right after the return, and
how long it took until it did.

Run this only on a workstation with SOLIDWORKS already running and nothing
else driving it:

    ..\\.venv\\Scripts\\python.exe tests\\live_activation.py --rounds 20
    ..\\.venv\\Scripts\\python.exe tests\\live_activation.py --saved C:\\path\\part.sldprt

Conditions:
  empty   an empty new part is active before the call
  built   an unsaved part with two features is active before the call
  saved   the saved part given with --saved is active before the call

Every document the script creates is closed again at the end of its round; the
saved part stays open.  Nothing is saved.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp.sw_feature import boss_extrude, cut_extrude
from solidworks_mcp.sw_file import close_document, create_new_document, get_active_document_info, open_document
from solidworks_mcp.sw_sketch import close_sketch, create_sketch, draw_circle, draw_rectangle

POLL_S = 0.05
LIMIT_S = 5.0


def require(payload: dict[str, Any], context: str) -> dict[str, Any]:
    if not payload.get("ok"):
        raise AssertionError(f"{context} failed: {payload.get('message')}")
    return payload


def active_title() -> str:
    info = get_active_document_info({})
    return str(((info.get("data") or {}).get("document") or {}).get("title") or "") if info.get("ok") else ""


def new_part() -> str:
    """A new part that is surely active before the measured call; returns its title."""
    answer = create_new_document({"kind": "part"})
    title = str((((answer.get("data") or {}).get("document")) or {}).get("title") or "")
    if not title:
        raise AssertionError(f"create predecessor failed: {answer.get('message')}")
    deadline = time.monotonic() + LIMIT_S
    while active_title() != title and time.monotonic() < deadline:
        time.sleep(POLL_S)
    if active_title() != title:
        raise AssertionError(f"predecessor {title} never became active")
    return title


def build_features() -> None:
    require(create_sketch({"plane": "front", "name": "LA_Base"}), "base sketch")
    require(draw_rectangle({"x1_mm": -40, "y1_mm": -30, "x2_mm": 40, "y2_mm": 30}), "base rectangle")
    require(close_sketch({}), "close base sketch")
    require(boss_extrude({"sketch_name": "LA_Base", "depth_mm": 12, "name": "LA_Boss"}), "boss")
    require(create_sketch({"plane": "front", "name": "LA_Hole"}), "hole sketch")
    require(draw_circle({"x_mm": 0, "y_mm": 0, "radius_mm": 8}), "hole circle")
    require(close_sketch({}), "close hole sketch")
    require(cut_extrude({"sketch_name": "LA_Hole", "end_condition": "through_all_both", "name": "LA_Cut"}), "cut")


def measure(condition: str, saved: str | None, observe_s: float = LIMIT_S) -> dict[str, Any]:
    created: list[str] = []
    if condition == "saved":
        require(open_document({"path": saved}), "activate saved part")
        before = active_title()
    else:
        before = new_part()
        created.append(before)
        if condition == "built":
            build_features()
    started = time.monotonic()
    answer = create_new_document({"kind": "part"})
    returned = time.monotonic()
    data = answer.get("data") or {}
    title = str((data.get("document") or {}).get("title") or "")
    if title:
        created.append(title)
    immediate = active_title()
    seen = immediate
    while seen != title and time.monotonic() - returned < observe_s:
        time.sleep(POLL_S)
        seen = active_title()
    after_return_ms = round((time.monotonic() - returned) * 1000) if seen == title else None
    row = {
        "condition": condition,
        "before": before,
        "new": title,
        "ok": bool(answer.get("ok")),
        "activated": data.get("activated"),
        "wait_ms": data.get("wait_ms"),
        "call_ms": round((returned - started) * 1000),
        "active_on_return": immediate == title,
        "active_on_return_title": immediate,
        "active_at_end_title": seen,
        "after_return_ms": after_return_ms,
    }
    for doc in reversed(created):
        close_document({"title": doc})
    return row


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for condition in sorted({r["condition"] for r in rows}):
        group = [r for r in rows if r["condition"] == condition]
        late = [r for r in group if not r["active_on_return"]]
        delays = [r["after_return_ms"] for r in late if r["after_return_ms"] is not None]
        calls = [r["call_ms"] for r in group]
        out[condition] = {
            "rounds": len(group),
            "not_active_on_return": len(late),
            "never_active_while_observed": sum(1 for r in late if r["after_return_ms"] is None),
            "late_delay_ms_median": statistics.median(delays) if delays else None,
            "late_delay_ms_max": max(delays) if delays else None,
            "call_ms_median": statistics.median(calls),
            "call_ms_max": max(calls),
            "not_ok": sum(1 for r in group if not r["ok"]),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--conditions", default="empty,built,saved")
    parser.add_argument("--saved", help="saved .sldprt for the 'saved' condition; without it that condition is skipped")
    parser.add_argument("--observe", type=float, default=LIMIT_S, help="seconds to watch for the new document after the return")
    parser.add_argument("--pause", type=float, default=0.0, help="idle seconds before every round")
    parser.add_argument("--json", help="write every round as JSON to this file")
    args = parser.parse_args()
    conditions = [c for c in args.conditions.split(",") if c and (c != "saved" or args.saved)]
    rows = []
    for condition in conditions:
        for _ in range(args.rounds):
            time.sleep(args.pause)
            row = measure(condition, args.saved, args.observe)
            rows.append(row)
            print(json.dumps(row), flush=True)
    result = {"summary": summary(rows), "rounds": rows}
    print(json.dumps(result["summary"], indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
