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

"""Live check: draw_hole_circle draws a centred hole-circle sketch in one call (ClauSW#78).

Per case one part: a plate 160 x 160 x 10 mm on the front plane (256 000 mm3), then a sketch on the front plane with
draw_hole_circle (pitch 108, hole 14.5) at the case's start angle, close_sketch and a through-all cut.  Cases: the
brake disc's 90 degrees and one angle per quadrant, 15, 105, 195 and 285 degrees.  Checks per case:

(a) the call answers ok true, fully_defined, angle_mode vertical for 90 and dimension otherwise, the dimensions
    Lochkreis 108, Bohrung 14.5 and, off the quadrants, Startwinkel at the angle below 180 and 360 - angle above,
    all driving; list_dimensions of the sketch names exactly these and none driven;
(b) after the cut the volume is 256 000 - pi * 7.25^2 * 10 mm3 and the hole centre in the closed sketch lies at
    r 54 and the start angle;
(c) pitch probe: set_dimension of Lochkreis to 100 answers ok, the hole centre lies at r 50 and the same angle, the
    volume is unchanged, check_errors with sketches true lists nothing; back to 108, volume as in (b).
(d) recorded, not asserted: every answer and the time of every draw_hole_circle call.

Run this only on a workstation with SOLIDWORKS already running and nothing else driving it:

    ..\\.venv\\Scripts\\python.exe tests\\live_hole_circle.py

Every part is closed without saving.  The output is one JSON object, also when a step raises; exit code 0 when (a)
to (c) hold for every case.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import solidworks_mcp.server  # noqa: E402,F401  (registers every tool)
from solidworks_mcp.sw_core import HANDLERS  # noqa: E402

PLATE = 160.0 * 160.0 * 10.0
CUT = PLATE - math.pi * 7.25 ** 2 * 10.0
CASES = [90, 15, 105, 195, 285]


def data(answer: dict[str, Any], key: str, default: Any = None) -> Any:
    return (answer.get("data") or {}).get(key, default)


def close_to(actual: float | None, wanted: float, tolerance: float = 1e-3) -> bool:
    return actual is not None and abs(float(actual) - wanted) <= tolerance


def angle_gap(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


class Run:
    def __init__(self) -> None:
        self.report: dict[str, Any] = {"steps": [], "draw_hole_circle_s": [], "checks": {}, "recorded": {}}

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        answer = HANDLERS[name](args or {})
        seconds = round(time.perf_counter() - started, 3)
        self.report["steps"].append({"tool": name, "args": args, "ok": answer.get("ok"), "message": answer.get("message"),
                                     "s": seconds})
        if name == "draw_hole_circle":
            self.report["draw_hole_circle_s"].append(seconds)
        return answer

    def require(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = self.call(name, args)
        if not answer.get("ok"):
            raise RuntimeError(f"{name} failed: {answer.get('message')}")
        return answer

    def new_part(self) -> str:
        created = self.require("create_new_document", {"kind": "part"})
        title = ((created.get("data") or {}).get("document") or {}).get("title")
        if not title:
            raise RuntimeError(f"create_new_document named no document: {created}")
        return str(title)

    def close_part(self, label: str, title: str | None) -> None:
        if title:
            self.report["steps"].append({f"close_{label}": self.call("close_document", {"title": title}).get("ok")})

    def volume(self) -> float | None:
        return data(self.call("get_mass_properties"), "volume_mm3")

    def hole_centre(self, sketch: str) -> dict[str, float] | None:
        rows = data(self.call("list_sketch_segments", {"sketch_name": sketch}), "segments", []) or []
        holes = [r for r in rows if r["type"] == "arc" and not r["construction"]]
        if len(holes) != 1:
            return None
        x, y = holes[0]["center_mm"][:2]
        return {"r_mm": math.hypot(x, y), "angle_deg": math.degrees(math.atan2(y, x)) % 360.0}

    def at(self, sketch: str, radius: float, angle: float) -> bool:
        centre = self.hole_centre(sketch)
        return (centre is not None and close_to(centre["r_mm"], radius, 1e-6)
                and angle_gap(centre["angle_deg"], angle) <= 1e-6)


def case(run: Run, angle: float) -> None:
    checks, recorded = run.report["checks"], run.report["recorded"]
    label, sketch = f"{angle:g}", f"L{angle:g}_Skizze"
    title = run.new_part()
    try:
        run.require("create_sketch", {"plane": "front", "name": "Platte_Skizze"})
        run.require("draw_rectangle", {"x1_mm": -80, "y1_mm": -80, "x2_mm": 80, "y2_mm": 80})
        run.require("close_sketch")
        run.require("boss_extrude", {"depth_mm": 10, "sketch_name": "Platte_Skizze"})
        run.require("create_sketch", {"plane": "front", "name": sketch})
        answer = run.call("draw_hole_circle", {"pitch_diameter_mm": 108, "hole_diameter_mm": 14.5,
                                               "start_angle_deg": angle})
        recorded[f"{label}_answer"] = answer
        run.require("close_sketch")
        quadrant = angle % 90 == 0
        wanted = {"Lochkreis": ("value_mm", 108.0), "Bohrung": ("value_mm", 14.5)}
        if not quadrant:
            wanted["Startwinkel"] = ("value_deg", angle if angle <= 180 else 360 - angle)
        dims = data(answer, "dimensions", []) or []
        got = {d.get("name"): d for d in dims}
        rows = data(run.call("list_dimensions", {"feature_name": sketch}), "dimensions", []) or []
        recorded[f"{label}_list_dimensions"] = rows
        checks[f"a_{label}_answer"] = (
            answer.get("ok") is True and data(answer, "sketch_status") == "fully_defined"
            and data(answer, "angle_mode") == ("vertical" if angle == 90 else "dimension")
            and set(got) == set(wanted)
            and all(close_to(got[n].get(k), v, 1e-9) and got[n].get("driven") is False for n, (k, v) in wanted.items())
            and sorted(str(r.get("name")) for r in rows) == sorted(wanted)
            and all(r.get("driven") is False for r in rows))
        run.require("cut_extrude", {"sketch_name": sketch, "end_condition": "through_all_both", "name": f"L{label}"})
        volume = run.volume()
        recorded[f"{label}_volume_mm3"] = volume
        checks[f"b_{label}_cut"] = close_to(volume, CUT) and run.at(sketch, 54, angle % 360)

        full = next((str(r["full_name"]) for r in rows if r.get("name") == "Lochkreis"), "")
        smaller = run.call("set_dimension", {"full_name": full, "value_mm": 100})
        errors = run.call("check_errors", {"sketches": True})
        moved = run.at(sketch, 50, angle % 360)
        volume_100 = run.volume()
        back = run.call("set_dimension", {"full_name": full, "value_mm": 108})
        volume_back = run.volume()
        recorded[f"{label}_pitch_probe"] = {"set_100": smaller, "check_errors": errors, "volume_100": volume_100,
                                            "set_108": back, "volume_108": volume_back}
        checks[f"c_{label}_pitch_probe"] = (
            bool(full) and smaller.get("ok") is True and moved and close_to(volume_100, CUT)
            and errors.get("ok") is True and not data(errors, "unsolved_sketches")
            and back.get("ok") is True and close_to(volume_back, CUT) and run.at(sketch, 54, angle % 360))
    finally:
        run.close_part(label, title)


def main() -> int:
    run = Run()
    try:
        for angle in CASES:
            case(run, angle)
    finally:
        print(json.dumps(run.report, indent=2, default=str))
    checks = run.report["checks"]
    return 0 if len(checks) == 3 * len(CASES) and all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
