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

"""Live check: add_dimension fails when the new dimension leaves the sketch unsolved (#15) or comes out driven (#22).

Parts A to C get a sketch on the front plane with a rectangle 80 × 50 from (0, 0).  A second horizontal dimension
on the same top line is the conflict: draw_rectangle does not tie the bottom line's length to the top line's
(measured 2026-10-24 on 2016 SP3, a dimension of 70 on the bottom line was simply applied).  SOLIDWORKS 2016 then
makes the new dimension driven and keeps the sketch solving (measured the same day: SetSystemValue3 status 3, the
value stays 80), so the unsolved-sketch rule is reached through part D, the unsolvable sketch of SW_MCP#14, and
(b) and (c) are the driven rule of SW_MCP#22.  Checks:

(a) control: a horizontal dimension on the top line to 90 answers ok true, applied true and driven false; after
    close_sketch and an extrude of 10 mm the volume is 45 000 mm³;
(b) conflict: top line dimensioned to 80, then a second horizontal dimension on the top line to 70 answers ok
    false with its full_name, verified not false and driven true, also in list_dimensions; the message says the
    geometry did not move when the sketch reads unsolved, otherwise applied is false and the message says
    "reference dimension"; the top line still measures 80;
(c) redundant but consistent: as (b) with 80; ok false, driven true, applied true, verified not false, a full_name
    and a message that says "reference dimension"; the answer's driven equals list_dimensions' driven for it;
(d) unsolved sketch: part D is the #14 reproduction (quadrilateral (0,0) (90,0) (70,50) (10,40), sketch_fillet
    R5/R8/R5/R8, fully_define_sketch, extrude 10); set_dimension of a R5 radius to 6 leaves P_Skizze no_solution;
    in the reopened sketch a horizontal dimension on the bottom line answers ok false with its full_name, an
    unsolved sketch_status and sketch_status_before, and a message that says "already" and "did not move";
(s) the open sketch's state is current: the state add_dimension reports in (b) and (c) is unsolved exactly when
    check_errors with sketches true lists the sketch after close_sketch (SW_MCP#15, step 4 of the brief);
(e) recorded, not asserted: every answer, `driven` of each new dimension from list_dimensions, the time of every
    add_dimension call.

Run this only on a workstation with SOLIDWORKS already running and nothing else driving it:

    ..\\.venv\\Scripts\\python.exe tests\\live_add_dimension.py

Every part is closed without saving.  The output is one JSON object, also when a step raises; exit code 0 when (a)
to (d) and (s) hold.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import solidworks_mcp.server  # noqa: E402,F401  (registers every tool)
from solidworks_mcp.sw_core import HANDLERS, UNSOLVED_SKETCH_STATUS  # noqa: E402

EPS = 1e-4


def data(answer: dict[str, Any], key: str, default: Any = None) -> Any:
    return (answer.get("data") or {}).get(key, default)


def close_to(actual: float | None, wanted: float, tolerance: float = 1e-3) -> bool:
    return actual is not None and abs(float(actual) - wanted) <= tolerance


class Run:
    def __init__(self) -> None:
        self.report: dict[str, Any] = {"steps": [], "add_dimension_s": [], "checks": {}, "recorded": {}}

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        answer = HANDLERS[name](args or {})
        seconds = round(time.perf_counter() - started, 3)
        self.report["steps"].append({"tool": name, "args": args, "ok": answer.get("ok"), "message": answer.get("message"),
                                     "s": seconds})
        if name == "add_dimension":
            self.report["add_dimension_s"].append(seconds)
        return answer

    def require(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = self.call(name, args)
        if not answer.get("ok"):
            raise RuntimeError(f"{name} failed: {answer.get('message')}")
        return answer

    def new_part(self) -> str:
        """A new part, or stop: without one the sketch would land in whatever document is active."""
        created = self.require("create_new_document", {"kind": "part"})
        title = ((created.get("data") or {}).get("document") or {}).get("title")
        if not title:
            raise RuntimeError(f"create_new_document named no document: {created}")
        return str(title)

    def close_part(self, label: str, title: str | None) -> None:
        if title:
            self.report["steps"].append({f"close_{label}": self.call("close_document", {"title": title}).get("ok")})

    def rectangle(self, sketch: str) -> None:
        self.require("create_sketch", {"plane": "front", "name": sketch})
        self.require("draw_rectangle", {"x1_mm": 0, "y1_mm": 0, "x2_mm": 80, "y2_mm": 50})

    def line(self, side: str) -> dict[str, Any]:
        """The rectangle's top, bottom or left line as list_sketch_segments reports it."""
        segments = data(self.call("list_sketch_segments"), "segments", []) or []
        lines = [s for s in segments if s["type"] == "line" and not s["construction"]]
        if side in ("top", "bottom"):
            pick = max if side == "top" else min
            y = pick(s["start_mm"][1] for s in lines)
            found = [s for s in lines if abs(s["start_mm"][1] - y) < EPS and abs(s["end_mm"][1] - y) < EPS]
        else:
            x = min(s["start_mm"][0] for s in lines)
            found = [s for s in lines if abs(s["start_mm"][0] - x) < EPS and abs(s["end_mm"][0] - x) < EPS]
        if len(found) != 1:
            raise RuntimeError(f"no single {side} line in {lines}")
        return found[0]

    def add(self, side: str, kind: str, value_mm: float | None, place: tuple[float, float]) -> dict[str, Any]:
        return self.call("add_dimension", {"selection": {"sketch_segments": [self.line(side)["index"]]}, "kind": kind,
                                           "value_mm": value_mm, "place_x_mm": place[0], "place_y_mm": place[1]})

    def driven(self, sketch: str, answer: dict[str, Any]) -> Any:
        rows = data(self.call("list_dimensions", {"feature_name": sketch}), "dimensions", []) or []
        wanted = str(data(answer, "full_name", "")).split("@")[:2]
        return next((r.get("driven") for r in rows if str(r.get("full_name", "")).split("@")[:2] == wanted), None)

    def unsolved_after_close(self, sketch: str) -> bool:
        self.call("close_sketch")
        rows = data(self.call("check_errors", {"sketches": True}), "unsolved_sketches", []) or []
        return any(row.get("sketch") == sketch for row in rows)


def length(segment: dict[str, Any]) -> float:
    (x1, y1), (x2, y2) = segment["start_mm"][:2], segment["end_mm"][:2]
    return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5


def control(run: Run) -> None:
    """(a)"""
    checks, recorded = run.report["checks"], run.report["recorded"]
    title = run.new_part()
    try:
        run.rectangle("A_Skizze")
        answer = run.add("top", "horizontal", 90, (40, 60))
        recorded["a_answer"] = answer
        run.call("close_sketch")
        run.require("boss_extrude", {"depth_mm": 10, "sketch_name": "A_Skizze"})
        volume = data(run.call("get_mass_properties"), "volume_mm3")
        recorded["a_volume_mm3"] = volume
        checks["a_control_applies"] = (answer.get("ok") is True and data(answer, "applied") is True
                                       and data(answer, "driven") is False and close_to(volume, 45000))
    finally:
        run.close_part("a", title)


def conflict(run: Run) -> None:
    """(b), (d) and (s) for (b)"""
    checks, recorded = run.report["checks"], run.report["recorded"]
    title = run.new_part()
    try:
        run.rectangle("B_Skizze")
        recorded["b_top"] = run.add("top", "horizontal", 80, (40, 60))
        answer = run.add("top", "horizontal", 70, (40, 70))
        recorded["b_answer"] = answer
        recorded["b_driven"] = run.driven("B_Skizze", answer)
        top = length(run.line("top"))
        recorded["b_top_length_mm"] = top
        status = data(answer, "sketch_status")
        message = str(answer.get("message"))
        by_status = status in UNSOLVED_SKETCH_STATUS and "did not move" in message
        by_driven = data(answer, "applied") is False and "reference dimension" in message
        checks["b_conflict_fails"] = (answer.get("ok") is False and bool(data(answer, "full_name"))
                                      and data(answer, "verified") is not False and data(answer, "driven") is True
                                      and recorded["b_driven"] is True and (by_status or by_driven)
                                      and close_to(top, 80))
        closed = run.unsolved_after_close("B_Skizze")
        recorded["b_unsolved_after_close"] = closed
        checks["s_state_current_b"] = (status in UNSOLVED_SKETCH_STATUS) == closed
    finally:
        run.close_part("b", title)


def redundant(run: Run) -> None:
    """(c) and (s) for (c)"""
    checks, recorded = run.report["checks"], run.report["recorded"]
    title = run.new_part()
    try:
        run.rectangle("C_Skizze")
        recorded["c_top"] = run.add("top", "horizontal", 80, (40, 60))
        answer = run.add("top", "horizontal", 80, (40, 70))
        recorded["c_answer"] = answer
        recorded["c_driven"] = run.driven("C_Skizze", answer)
        status = data(answer, "sketch_status")
        checks["c_driven_fails"] = (answer.get("ok") is False and data(answer, "driven") is True
                                    and data(answer, "applied") is True and bool(data(answer, "full_name"))
                                    and data(answer, "verified") is not False
                                    and "reference dimension" in str(answer.get("message"))
                                    and recorded["c_driven"] is True)
        closed = run.unsolved_after_close("C_Skizze")
        recorded["c_unsolved_after_close"] = closed
        checks["s_state_current_c"] = (status in UNSOLVED_SKETCH_STATUS) == closed
    finally:
        run.close_part("c", title)


CORNERS = [(0, 0, 5), (90, 0, 8), (70, 50, 5), (10, 40, 8)]


def unsolvable(run: Run) -> None:
    """(d)"""
    checks, recorded = run.report["checks"], run.report["recorded"]
    title = run.new_part()
    try:
        run.require("create_sketch", {"plane": "front", "name": "P_Skizze"})
        for index, (x1, y1, _) in enumerate(CORNERS):
            x2, y2, _ = CORNERS[(index + 1) % len(CORNERS)]
            run.require("draw_line", {"x1_mm": x1, "y1_mm": y1, "x2_mm": x2, "y2_mm": y2})
        for x, y, radius in CORNERS:
            segments = data(run.call("list_sketch_segments"), "segments", []) or []
            lines = [s["index"] for s in segments if s["type"] == "line" and not s["construction"]
                     and any(abs(p[0] - x) < EPS and abs(p[1] - y) < EPS for p in (s["start_mm"], s["end_mm"]))]
            run.require("sketch_fillet", {"radius_mm": radius, "selection": {"sketch_segments": lines}})
        run.require("fully_define_sketch", {})
        run.require("close_sketch")
        run.require("boss_extrude", {"depth_mm": 10, "sketch_name": "P_Skizze"})
        rows = data(run.call("list_dimensions", {"feature_name": "P_Skizze"}), "dimensions", []) or []
        radius = next(r["full_name"] for r in rows if r.get("driven") is False and abs(float(r["value_mm"]) - 5) < 1e-6)
        recorded["d_set_dimension"] = run.call("set_dimension", {"full_name": radius, "value_mm": 6})
        run.require("edit_sketch", {"sketch_name": "P_Skizze"})
        answer = run.add("bottom", "horizontal", None, (45, -10))
        recorded["d_answer"] = answer
        run.call("close_sketch")
        message = str(answer.get("message"))
        checks["d_unsolved_sketch_fails"] = (
            data(recorded["d_set_dimension"], "sketch_status") == "no_solution"
            and answer.get("ok") is False and bool(data(answer, "full_name"))
            and data(answer, "sketch_status") in UNSOLVED_SKETCH_STATUS
            and data(answer, "sketch_status_before") in UNSOLVED_SKETCH_STATUS
            and "already" in message and "did not move" in message)
    finally:
        run.close_part("d", title)


def main() -> int:
    run = Run()
    try:
        control(run)
        conflict(run)
        redundant(run)
        unsolvable(run)
    finally:
        print(json.dumps(run.report, indent=2, default=str))
    checks = run.report["checks"]
    return 0 if checks and all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
