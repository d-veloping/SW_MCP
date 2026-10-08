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

"""Live measurement: which document is active right after open_document of a part and create_drawing.

ClauSW issue #115 saw the part that open_document had just opened become active again after create_drawing had
reported its new drawing as active (or never let the drawing become active at all).  Per round this script calls
open_document on `--part`, then create_drawing, and samples get_active_document_info every 20 ms for 5 s, recording
every change of the active title with its time in ms.  It also records the answers of both tools and, if the part is
active again, whether activate_document brings the drawing back.  `--no-second-activate` measures open_document
without its ActivateDoc3 when OpenDoc6 already made the part active.

Run this only on a workstation with SOLIDWORKS already running and nothing else driving it, on a copy of a saved
part that is not open; the caller holds whatever lock its environment uses:

    ..\\.venv\\Scripts\\python.exe tests\\live_drawing_activation.py --part C:\\tmp\\Test_Rahmen.sldprt --rounds 10

Drawing and part are closed without saving at the end of every round.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unittest.mock
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_file
from solidworks_mcp.sw_drawing import create_drawing
from solidworks_mcp.sw_file import activate_document, close_document, get_active_document_info, open_document

SAMPLE_S = 5.0
SAMPLE_EVERY_S = 0.02


def active_title() -> str | None:
    try:
        answer = get_active_document_info({})
    except Exception as exc:  # noqa: BLE001 - a measurement records, it does not stop
        return f"<{type(exc).__name__}>"
    return ((answer.get("data") or {}).get("document") or {}).get("title")


def timeline(start: float) -> list[tuple[int, str | None]]:
    """Every change of the active title within SAMPLE_S, as (ms since `start`, title)."""
    changes: list[tuple[int, str | None]] = []
    last = object()
    while time.perf_counter() - start < SAMPLE_S:
        title = active_title()
        if title != last:
            changes.append((round((time.perf_counter() - start) * 1000), title))
            last = title
        time.sleep(SAMPLE_EVERY_S)
    return changes


def short(answer: dict[str, Any]) -> dict[str, Any]:
    data = answer.get("data") or {}
    return {"ok": bool(answer.get("ok")), "title": (data.get("document") or {}).get("title"),
            "wait_ms": data.get("wait_ms"), "active": (data.get("active_document") or {}).get("title"),
            "message": str(answer.get("message"))[:200]}


def open_without_second_activate(path: str) -> dict[str, Any]:
    """open_document, but ActivateDoc3 only when OpenDoc6 did not already make the part the active document."""
    real = sw_file.running_app()

    class App:
        def __getattr__(self, name):
            return getattr(real, name)

        def ActivateDoc3(self, *args):  # noqa: N802 - COM member name
            active = real.ActiveDoc
            if active is not None and str(sw_file.value(active, "GetPathName") or "").lower() == path.lower():
                return True
            return real.ActivateDoc3(*args)
    with unittest.mock.patch.object(sw_file, "running_app", return_value=App()):
        return open_document({"path": path})


def one_round(part: str, no_second_activate: bool) -> dict[str, Any]:
    start = time.perf_counter()
    opened = open_without_second_activate(part) if no_second_activate else open_document({"path": part})
    drawn = create_drawing({})
    after_create = round((time.perf_counter() - start) * 1000)
    changes = timeline(start)
    drawing = ((drawn.get("data") or {}).get("document") or {}).get("title")
    row: dict[str, Any] = {"open": short(opened), "create": short(drawn), "create_done_ms": after_create,
                           "timeline": changes}
    stem = Path(part).stem.lower()

    def is_part(title: str | None) -> bool:
        return bool(title) and str(title).lower() in (stem, Path(part).name.lower())
    # the timeline starts after create_drawing returned: any part entry is the part active again
    row["part_active_again"] = bool(drawing) and any(is_part(title) for _, title in changes)
    row["part_active_at_end"] = bool(changes) and is_part(changes[-1][1])
    if drawing and row["part_active_at_end"]:
        row["reactivate"] = short(activate_document({"title": drawing}))
    for title in (drawing, part):
        if title:
            close_document({"title": title})
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--part", required=True)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--pause", type=float, default=0.0, help="seconds to wait before every round")
    parser.add_argument("--no-second-activate", action="store_true")
    parser.add_argument("--json", help="append every round as one JSON line to this file")
    args = parser.parse_args()
    part = str(Path(args.part).resolve())
    rows = []
    for number in range(1, args.rounds + 1):
        time.sleep(args.pause)
        row = {"round": number, "part": Path(part).name, "no_second_activate": args.no_second_activate,
               **one_round(part, args.no_second_activate)}
        rows.append(row)
        print(json.dumps(row), flush=True)
        if args.json:
            with open(args.json, "a", encoding="utf-8") as out:
                out.write(json.dumps(row) + "\n")
    summary = {"rounds": len(rows), "create_not_ok": sum(not r["create"]["ok"] for r in rows),
               "part_active_again": sum(r["part_active_again"] for r in rows),
               "part_active_at_end": sum(r["part_active_at_end"] for r in rows),
               "reactivated": sum(bool(r.get("reactivate", {}).get("ok")) for r in rows)}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
