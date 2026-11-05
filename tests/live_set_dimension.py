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

"""Live check: set_dimension fails when the owning sketch cannot solve, and check_errors can report it (#14).

Part F reproduces the issue: a closed quadrilateral (0,0) (90,0) (70,50) (10,40), sketch_fillet R5/R8/R5/R8 at the
corners, fully_define_sketch, extrude 10 mm (33 505.8996 mm³).  Changing a R5 radius to 6 leaves the sketch
no_solution on SOLIDWORKS 2016 SP3 and moves nothing.  Part K is the control: rectangle 80 × 50, fully defined,
extrude 10 mm (40 000 mm³), where changes do apply.  Checks:

(a) F: the radius change answers ok false, sketch_status no_solution, sketch_status_before fully_defined,
    actual_value_mm ≈ 5;
(b) F: the volume is unchanged afterwards;
(c) F: check_errors {} answers problems [] and sketches_checked false; with sketches true it answers ok false and
    names P_Skizze as no_solution;
(d) K: 80 → 90 answers ok true, fully_defined, and the volume is 45 000;
(e) K, open sketch: edit_sketch, 90 → 85 answers ok true with a sketch_status; close_sketch, volume 42 500.  The
    state reads fully_defined before and after, so this check does not tell whether an open sketch's state lags;
(f) K, feature dimension: the extrude depth 10 → 12 answers ok true, volume 51 000;
(g) K with its sketch open: setting the extrude depth answers ok false, the depth stays 12, volume unchanged;
(i) a second part F, open path: edit_sketch, the radius change inside the sketch answers ok false with
    sketch_status no_solution and actual_value_mm ≈ 5; after close_sketch the sketch solves again
    (unsolved_sketches []) and the volume is unchanged.
(j) diametric (#75): part L has a centerline on the x axis and a line at y = 25 with a linear dimension 25 to the
    centerline.  set_dimension with diametric true alone, in the open sketch and again after close_sketch, and with
    diametric true and value_mm 50 in the open sketch, each answers either ok false with the line still at 25 and the
    dimension still 25, or ok true with diametric true and the line still at 25 (a real diameter);
(k) parentheses (ClauSW#156): part K again.  parentheses true alone on the width 80 answers ok true with
    previous_parentheses false, parentheses true, driven false before and after and the value 80 before and after;
    the width then reads ShowParenthesis true and the height 50 false, volume 40 000.  The same on the extrude depth
    (a feature dimension).  value_mm 60 with parentheses true on the height applies both, volume 48 000.  parentheses
    false on the width answers ok true and the width reads false again, still 80 and driving, volume 48 000;
(h) recorded, not asserted: F's recovery edit_sketch → set_dimension 5 → close_sketch, then the sketch state and the
    volume; and the time of every set_dimension call.

Run this only on a workstation with SOLIDWORKS already running and nothing else driving it:

    ..\\.venv\\Scripts\\python.exe tests\\live_set_dimension.py

Every part is closed without saving.  The output is one JSON object, also when a step raises; exit code 0 when (a)
to (g) and (i) to (k) hold.
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
from solidworks_mcp.sw_core import HANDLERS, active_document  # noqa: E402
from solidworks_mcp.sw_sketch import _display_dimension, _display_flag  # noqa: E402

CORNERS = [(0, 0, 5), (90, 0, 8), (70, 50, 5), (10, 40, 8)]
F_VOLUME = 33505.8996
EPS = 1e-4


def data(answer: dict[str, Any], key: str, default: Any = None) -> Any:
    return (answer.get("data") or {}).get(key, default)


class Run:
    def __init__(self) -> None:
        self.report: dict[str, Any] = {"steps": [], "set_dimension_s": [], "checks": {}, "recorded": {}}

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        answer = HANDLERS[name](args or {})
        seconds = round(time.perf_counter() - started, 3)
        self.report["steps"].append({"tool": name, "args": args, "ok": answer.get("ok"), "message": answer.get("message"),
                                     "s": seconds})
        if name == "set_dimension":
            self.report["set_dimension_s"].append(seconds)
        return answer

    def volume(self) -> float | None:
        return data(self.call("get_mass_properties"), "volume_mm3")

    def segments(self) -> list[dict[str, Any]]:
        return data(self.call("list_sketch_segments"), "segments", []) or []

    def dimension(self, feature: str, value_mm: float) -> str:
        rows = data(self.call("list_dimensions", {"feature_name": feature}), "dimensions", []) or []
        for row in rows:
            if row.get("driven") is False and abs(float(row.get("value_mm", -1)) - value_mm) < 1e-6:
                return str(row["full_name"])
        raise RuntimeError(f"no driving dimension of {value_mm} mm in {feature}: {rows}")

    def new_part(self) -> str | None:
        created = self.call("create_new_document", {"kind": "part"})
        return ((created.get("data") or {}).get("document") or {}).get("title")

    def close_part(self, label: str, title: str | None) -> None:
        if title:
            self.report["steps"].append({f"close_{label}": self.call("close_document", {"title": title}).get("ok")})

    def build_f(self) -> None:
        self.call("create_sketch", {"plane": "front", "name": "P_Skizze"})
        for index, (x1, y1, _) in enumerate(CORNERS):
            x2, y2, _ = CORNERS[(index + 1) % len(CORNERS)]
            self.call("draw_line", {"x1_mm": x1, "y1_mm": y1, "x2_mm": x2, "y2_mm": y2})
        for x, y, radius in CORNERS:
            lines = [s["index"] for s in self.segments() if s["type"] == "line" and not s["construction"]
                     and (near(s["start_mm"], (x, y)) or near(s["end_mm"], (x, y)))]
            self.call("sketch_fillet", {"radius_mm": radius, "selection": {"sketch_segments": lines}})
        self.call("fully_define_sketch", {})
        self.call("close_sketch")
        self.call("boss_extrude", {"depth_mm": 10, "sketch_name": "P_Skizze"})

    def build_k(self) -> str:
        self.call("create_sketch", {"plane": "front", "name": "K_Skizze"})
        self.call("draw_rectangle", {"x1_mm": 0, "y1_mm": 0, "x2_mm": 80, "y2_mm": 50})
        self.call("fully_define_sketch", {})
        self.call("close_sketch")
        return str(data(self.call("boss_extrude", {"depth_mm": 10, "sketch_name": "K_Skizze"}), "feature", ""))


def near(p: list[float], q: tuple[float, float]) -> bool:
    return abs(p[0] - q[0]) < EPS and abs(p[1] - q[1]) < EPS


def close_to(actual: float | None, wanted: float, tolerance: float = 1e-3) -> bool:
    return actual is not None and abs(float(actual) - wanted) <= tolerance


def part_f(run: Run) -> None:
    """The reproduction, closed path: (a), (b), (c) and the recording (h)."""
    checks, recorded = run.report["checks"], run.report["recorded"]
    title = run.new_part()
    try:
        run.build_f()
        checks["f_built"] = close_to(run.volume(), F_VOLUME)
        name = run.dimension("P_Skizze", 5)
        changed = run.call("set_dimension", {"full_name": name, "value_mm": 6})
        recorded["a_answer"] = changed
        checks["a_unsolved_reported"] = (changed.get("ok") is False and data(changed, "sketch_status") == "no_solution"
                                         and data(changed, "sketch_status_before") == "fully_defined"
                                         and close_to(data(changed, "actual_value_mm"), 5, 1e-6))
        checks["b_volume_unchanged"] = close_to(run.volume(), F_VOLUME)
        plain = run.call("check_errors", {})
        with_sketches = run.call("check_errors", {"sketches": True})
        recorded["c_answers"] = [plain, with_sketches]
        checks["c_check_errors"] = (data(plain, "problems") == [] and data(plain, "sketches_checked") is False
                                    and with_sketches.get("ok") is False
                                    and data(with_sketches, "unsolved_sketches") == [{"sketch": "P_Skizze",
                                                                                      "sketch_status": "no_solution"}])
        run.call("edit_sketch", {"sketch_name": "P_Skizze"})
        recorded["h_open_status"] = data(run.call("get_sketch_status"), "sketch_status")
        recorded["h_set_back"] = run.call("set_dimension", {"full_name": name, "value_mm": 5})
        run.call("close_sketch")
        recorded["h_after"] = {"check_errors": run.call("check_errors", {"sketches": True}), "volume": run.volume()}
    finally:
        run.close_part("f", title)


def part_k(run: Run) -> None:
    """The control: (d), (e), (f), (g)."""
    checks, recorded = run.report["checks"], run.report["recorded"]
    title = run.new_part()
    try:
        extrude = run.build_k()
        checks["k_built"] = close_to(run.volume(), 40000)
        width = run.dimension("K_Skizze", 80)
        changed = run.call("set_dimension", {"full_name": width, "value_mm": 90})
        recorded["d_answer"] = changed
        checks["d_closed_change_applies"] = (changed.get("ok") is True and data(changed, "sketch_status") == "fully_defined"
                                             and close_to(run.volume(), 45000))
        run.call("edit_sketch", {"sketch_name": "K_Skizze"})
        changed = run.call("set_dimension", {"full_name": width, "value_mm": 85})
        recorded["e_answer"] = changed
        run.call("close_sketch")
        checks["e_open_change_applies"] = (changed.get("ok") is True and "sketch_status" in (changed.get("data") or {})
                                           and close_to(run.volume(), 42500))
        depth = run.dimension(extrude, 10)
        changed = run.call("set_dimension", {"full_name": depth, "value_mm": 12})
        recorded["f_answer"] = changed
        checks["f_feature_dimension"] = changed.get("ok") is True and close_to(run.volume(), 51000)
        run.call("edit_sketch", {"sketch_name": "K_Skizze"})
        blocked = run.call("set_dimension", {"full_name": depth, "value_mm": 14})
        recorded["g_answer"] = blocked
        run.call("close_sketch")
        rows = data(run.call("list_dimensions", {"feature_name": extrude}), "dimensions", []) or []
        depth_now = next((r.get("value_mm") for r in rows if r["full_name"] == depth), None)
        checks["g_other_sketch_open_blocks"] = (blocked.get("ok") is False and close_to(depth_now, 12, 1e-6)
                                                and close_to(run.volume(), 51000))
    finally:
        run.close_part("k", title)


def part_f_open(run: Run) -> None:
    """The reproduction again, open path: (i)."""
    checks, recorded = run.report["checks"], run.report["recorded"]
    title = run.new_part()
    try:
        run.build_f()
        name = run.dimension("P_Skizze", 5)
        run.call("edit_sketch", {"sketch_name": "P_Skizze"})
        changed = run.call("set_dimension", {"full_name": name, "value_mm": 6})
        recorded["i_answer"] = changed
        run.call("close_sketch")
        after = run.call("check_errors", {"sketches": True})
        recorded["i_after_close"] = after
        checks["i_open_path_rejects"] = (changed.get("ok") is False and data(changed, "sketch_status") == "no_solution"
                                         and close_to(data(changed, "actual_value_mm"), 5, 1e-6)
                                         and data(after, "unsolved_sketches") == [] and close_to(run.volume(), F_VOLUME))
    finally:
        run.close_part("f_open", title)


def part_l(run: Run) -> None:
    """(j)"""
    checks, recorded = run.report["checks"], run.report["recorded"]
    title = run.new_part()
    try:
        run.call("create_sketch", {"plane": "front", "name": "L_Skizze"})
        run.call("draw_centerline", {"x1_mm": -5, "y1_mm": 0, "x2_mm": 60, "y2_mm": 0})
        run.call("draw_line", {"x1_mm": 0, "y1_mm": 25, "x2_mm": 50, "y2_mm": 25})
        segments = run.segments()
        centre = next(s["index"] for s in segments if s["construction"])
        line = next(s["index"] for s in segments if not s["construction"])
        added = run.call("add_dimension", {"selection": {"sketch_segments": [line, centre]}, "value_mm": 25,
                                           "place_x_mm": -10, "place_y_mm": 12})
        recorded["j_added"] = added
        name = str(data(added, "full_name", ""))

        def line_y(sketch_open: bool) -> float:
            rows = (run.segments() if sketch_open else
                    data(run.call("list_sketch_segments", {"sketch_name": "L_Skizze"}), "segments", []) or [])
            return float(next(s for s in rows if s["index"] == line)["start_mm"][1])

        def judged(answer: dict[str, Any], y: float) -> bool:
            rows = data(run.call("list_dimensions", {"feature_name": "L_Skizze"}), "dimensions", []) or []
            still_25 = any(str(r.get("full_name", "")).split("@")[0] == name.split("@")[0]
                           and close_to(r.get("value_mm"), 25, 1e-6) for r in rows)
            refused = answer.get("ok") is False and still_25
            real = answer.get("ok") is True and data(answer, "diametric") is True
            return (refused or real) and close_to(y, 25, 1e-6)

        results = {}
        for label, args, sketch_open in (("open_display", {"diametric": True}, True),
                                         ("open_display_value", {"diametric": True, "value_mm": 50}, True),
                                         ("closed_display", {"diametric": True}, False)):
            if not sketch_open:
                run.call("close_sketch")
            answer = run.call("set_dimension", {"full_name": name, **args})
            y = line_y(sketch_open)
            recorded[f"j_{label}"] = {"answer": answer, "line_y_mm": y}
            results[label] = judged(answer, y)
        checks["j_diametric_never_moves_the_line"] = bool(name) and added.get("ok") is True and all(results.values())
        recorded["j_results"] = results
    finally:
        run.close_part("l", title)


def parentheses_now(name: str) -> bool | None:
    """ShowParenthesis of the model dimension as SOLIDWORKS reads it now, found afresh through the feature tree."""
    _, doc = active_document()
    display = _display_dimension(doc, name)
    return None if display is None else _display_flag(display, "ShowParenthesis")


def part_p(run: Run) -> None:
    """(k)"""
    checks, recorded = run.report["checks"], run.report["recorded"]
    title = run.new_part()
    try:
        extrude = run.build_k()
        checks["k_p_built"] = close_to(run.volume(), 40000)
        width, height, depth = run.dimension("K_Skizze", 80), run.dimension("K_Skizze", 50), run.dimension(extrude, 10)

        def unchanged(answer: dict[str, Any], before: bool, after: bool, value_mm: float) -> bool:
            return (answer.get("ok") is True
                    and (data(answer, "previous_parentheses"), data(answer, "parentheses")) == (before, after)
                    and (data(answer, "previous_driven"), data(answer, "driven")) == (False, False)
                    and close_to(data(answer, "previous_value_mm"), value_mm, 1e-6)
                    and close_to(data(answer, "actual_value_mm"), value_mm, 1e-6))

        on = run.call("set_dimension", {"full_name": width, "parentheses": True})
        recorded["k_on"] = on
        states = {"width": parentheses_now(width), "height": parentheses_now(height)}
        recorded["k_on_states"] = states
        checks["k_parentheses_on"] = (unchanged(on, False, True, 80) and states == {"width": True, "height": False}
                                      and close_to(run.volume(), 40000))

        feature = run.call("set_dimension", {"full_name": depth, "parentheses": True})
        recorded["k_feature"] = feature
        checks["k_feature_dimension"] = (unchanged(feature, False, True, 10) and parentheses_now(depth) is True
                                         and close_to(run.volume(), 40000))

        both = run.call("set_dimension", {"full_name": height, "value_mm": 60, "parentheses": True})
        recorded["k_value_and_parentheses"] = both
        checks["k_value_and_parentheses"] = (both.get("ok") is True and data(both, "parentheses") is True
                                             and close_to(data(both, "actual_value_mm"), 60, 1e-6)
                                             and data(both, "driven") is False and parentheses_now(height) is True
                                             and close_to(run.volume(), 48000))

        off = run.call("set_dimension", {"full_name": width, "parentheses": False})
        recorded["k_off"] = off
        rows = data(run.call("list_dimensions", {"feature_name": "K_Skizze"}), "dimensions", []) or []
        recorded["k_rows_after"] = rows
        width_row = next((r for r in rows if r.get("full_name") == width), {})
        checks["k_parentheses_off"] = (unchanged(off, True, False, 80) and parentheses_now(width) is False
                                       and width_row.get("driven") is False
                                       and close_to(width_row.get("value_mm"), 80, 1e-6)
                                       and close_to(run.volume(), 48000))
    finally:
        run.close_part("p", title)


def main() -> int:
    run = Run()
    try:
        part_f(run)
        part_k(run)
        part_f_open(run)
        part_l(run)
        part_p(run)
    finally:
        print(json.dumps(run.report, indent=2, default=str))
    checks = run.report["checks"]
    return 0 if checks and all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
