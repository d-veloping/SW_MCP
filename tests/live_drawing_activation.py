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
reported its new drawing as active (or never let the drawing become active at all).  Per round this script optionally
runs `--before` (for example a build that saves and closes the part), calls open_document on `--part`, then
create_drawing with the arguments the ClauSW drawing step uses (A3, first-angle), and samples
get_active_document_info every 20 ms for 5 s after create_drawing returned, recording every change of the active
title with its time in ms since the round started.  It also records the answers of both tools and, if the part is
active at the end and the server has activate_document, whether that brings the drawing back.
`--no-second-activate` measures open_document without its ActivateDoc3 when OpenDoc6 already made the part active.

The script also runs against an older server without activate_document (then nothing is brought back); an
exception in a round is recorded in that round, and the round's documents are closed in any case.  A round whose
documents cannot be closed ends the run, because every later round would start from a different state.

Run this only on a workstation with SOLIDWORKS already running and nothing else driving it, on a copy of a saved
part that is not open; the caller holds whatever lock its environment uses (for ClauSW: the bauplan lock, released
around a `--before` build, which takes it itself):

    ..\\.venv\\Scripts\\python.exe tests\\live_drawing_activation.py --part C:\\tmp\\Test_Rahmen.sldprt --rounds 10
    ..\\.venv\\Scripts\\python.exe tests\\live_drawing_activation.py --part C:\\tmp\\Test_Rahmen.sldprt --rounds 20 ^
        --before "bauplan C:\\tmp\\test_rahmen.json --ordner C:\\tmp" --json C:\\tmp\\messung.jsonl
"""

from __future__ import annotations

import argparse
import json
import subprocess
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
from solidworks_mcp.sw_file import close_document, get_active_document_info, open_document

activate_document = getattr(sw_file, "activate_document", None)   # missing on servers before ClauSW #115

SAMPLE_S = 5.0
SAMPLE_EVERY_S = 0.02
DRAWING_ARGS = {"paper_size": "A3", "first_angle": True}   # as bauplan/zeichnung.py calls it


def active_title() -> str | None:
    try:
        answer = get_active_document_info({})
    except Exception as exc:  # noqa: BLE001 - a measurement records, it does not stop
        return f"<{type(exc).__name__}>"
    return ((answer.get("data") or {}).get("document") or {}).get("title")


def timeline(start: float, until: float) -> list[tuple[int, str | None]]:
    """Every change of the active title until `until` (perf_counter), as (ms since `start`, title)."""
    changes: list[tuple[int, str | None]] = []
    last = object()
    while time.perf_counter() < until:
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


def is_part(title: str | None, part: str) -> bool:
    return bool(title) and str(title).lower() in (Path(part).stem.lower(), Path(part).name.lower())


def one_round(part: str, no_second_activate: bool) -> dict[str, Any]:
    row: dict[str, Any] = {}
    drawing = None
    try:
        start = time.perf_counter()
        opened = open_without_second_activate(part) if no_second_activate else open_document({"path": part})
        row["open"] = short(opened)
        drawn = create_drawing(dict(DRAWING_ARGS))
        row["create"] = short(drawn)
        drawing = ((drawn.get("data") or {}).get("document") or {}).get("title")
        row["create_done_ms"] = round((time.perf_counter() - start) * 1000)
        changes = timeline(start, time.perf_counter() + SAMPLE_S)
        row["timeline"] = changes
        # the timeline starts after create_drawing returned: any part entry is the part active again
        row["part_active_again"] = bool(drawing) and any(is_part(title, part) for _, title in changes)
        row["part_active_at_end"] = bool(changes) and is_part(changes[-1][1], part)
        if drawing and row["part_active_at_end"] and activate_document is not None:
            row["reactivate"] = short(activate_document({"title": drawing}))
    except Exception as exc:  # noqa: BLE001 - recorded per round, the documents are still closed below
        row["exception"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        row["closed"] = {}
        for title in (drawing, part):
            if title:
                try:
                    row["closed"][str(title)] = bool(close_document({"title": title}).get("ok"))
                except Exception as exc:  # noqa: BLE001
                    row["closed"][str(title)] = f"{type(exc).__name__}: {exc}"[:200]
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--part", required=True)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--pause", type=float, default=0.0, help="seconds to wait before every round")
    parser.add_argument("--before", help="command run before every round, e.g. a build that saves and closes the part")
    parser.add_argument("--no-second-activate", action="store_true")
    parser.add_argument("--json", help="append every round as one JSON line to this file")
    args = parser.parse_args()
    part = str(Path(args.part).resolve())
    rows = []
    for number in range(1, args.rounds + 1):
        time.sleep(args.pause)
        row: dict[str, Any] = {"round": number, "part": Path(part).name, "no_second_activate": args.no_second_activate,
                               "activate_document": activate_document is not None}
        if args.before:
            done = subprocess.run(args.before, shell=True, capture_output=True, text=True, errors="replace")
            row["before_exit"] = done.returncode
            if done.returncode != 0:
                row["before_output"] = (done.stdout + done.stderr)[-500:]
        row.update(one_round(part, args.no_second_activate))
        rows.append(row)
        print(json.dumps(row), flush=True)
        if args.json:
            with open(args.json, "a", encoding="utf-8") as out:
                out.write(json.dumps(row) + "\n")
        if any(value is not True for value in row["closed"].values()):
            print("A document of this round could not be closed; stopping.", file=sys.stderr)
            break
    measured = [r for r in rows if "create" in r]
    summary = {"rounds": len(rows), "exceptions": sum("exception" in r for r in rows),
               "create_not_ok": sum(not r["create"]["ok"] for r in measured),
               "part_active_again": sum(bool(r.get("part_active_again")) for r in measured),
               "part_active_at_end": sum(bool(r.get("part_active_at_end")) for r in measured),
               "reactivated": sum(bool((r.get("reactivate") or {}).get("ok")) for r in measured)}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
