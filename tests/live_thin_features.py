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

"""Live check: thin features on boss_extrude, cut_extrude and revolve (#37).

Per case one part, closed without saving. Volume and exact bounding box are read back and compared with closed-form
values; a sharp corner of a thin wall along an open L of legs a and b with thickness t has the area t * (a + b) when
the wall is centred on the contour, t * (a + b) + t^2 on the outer and t * (a + b) - t^2 on the inner side.

(a) L, open contour (0,40) -> (0,0) -> (60,0) on the front plane, depth 20, thin 5 mid_plane: 10 000 mm3, box
    62.5 x 42.5 x 20.
(b) the same L with one_side and with reverse: together the volumes {10 500, 9 500} with the boxes 65 x 45 x 20 and
    60 x 40 x 20, one each; which side is which depends on the sketch orientation and is recorded only.
(c) the same L two_side with 5 and 2: (700 + 25 - 4) * 20 or (700 - 25 + 4) * 20 mm3.
(d) sleeve: centerline on x, open line (0,20) -> (50,20), revolve 360 degrees, thin 3 mid_plane: 6000 pi mm3, box
    50 x 43 x 43; and the solid revolve of the closed rectangle (0,18.5) -> (50,21.5) gives the same volume.
(e) thin cut: block 60 x 40 x 20 (48 000 mm3), circle r 10 on the front plane, cut_extrude depth 5, thin 2 mid_plane:
    48 000 - 200 pi mm3, box unchanged.
(f) every thin answer is ok true and reports thin_mm and thin_side.

Geprüft am 2026-11-07 on SOLIDWORKS 2016 SP3 with direct calls, before the handlers used them: FeatureExtrusionThin2
builds the L with RevThinDir 2 (10 000 mm3), 0 (10 500) and 1 (9 500); FeatureRevolve2 with IsSolid true and IsThin
true builds the sleeve (6000 pi mm3), with IsSolid false it returns a feature but no solid body; FeatureCutThin2 and
FeatureCutThin cut the groove (48 000 - 200 pi mm3) and return None when the direction points away from the body,
which cut_extrude's retry in the other direction covers.

Run this only on a workstation with SOLIDWORKS already running and nothing else driving it:

    ..\\.venv\\Scripts\\python.exe tests\\live_thin_features.py

The output is one JSON object, also when a step raises; exit code 0 when every check holds.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import solidworks_mcp.server  # noqa: E402,F401  (registers every tool)
from solidworks_mcp.sw_core import HANDLERS  # noqa: E402

L_POINTS = [(0.0, 40.0), (0.0, 0.0), (60.0, 0.0)]
SLEEVE = 6000.0 * math.pi
CHECKS = ("a_l_mid", "b_l_sides", "c_l_two_side", "d_sleeve_thin", "d_sleeve_solid", "e_thin_cut", "f_answers")


def data(answer: dict[str, Any], key: str, default: Any = None) -> Any:
    return (answer.get("data") or {}).get(key, default)


def close_to(actual: Any, wanted: float, relative: float = 1e-6) -> bool:
    return isinstance(actual, (int, float)) and abs(float(actual) - wanted) <= relative * max(1.0, abs(wanted))


def size_is(size: Any, wanted: tuple[float, float, float]) -> bool:
    return isinstance(size, list) and len(size) == 3 and sorted(size) == sorted(wanted) and all(
        close_to(a, b) for a, b in zip(sorted(size), sorted(wanted)))


class Run:
    def __init__(self) -> None:
        self.report: dict[str, Any] = {"steps": [], "checks": {}, "recorded": {}}
        self.thin_answers: list[dict[str, Any]] = []

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = HANDLERS[name](args or {})
        self.report["steps"].append({"tool": name, "args": args, "ok": answer.get("ok"),
                                     "message": answer.get("message")})
        if (args or {}).get("thin_mm") is not None:
            self.thin_answers.append(answer)
        return answer

    def require(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = self.call(name, args)
        if not answer.get("ok"):
            raise RuntimeError(f"{name} failed: {answer.get('message')}")
        return answer

    def part(self, label: str, build: Callable[[], None]) -> tuple[Any, Any]:
        """Build in a fresh part and read volume and exact box; the part is closed without saving."""
        created = self.require("create_new_document", {"kind": "part"})
        title = ((created.get("data") or {}).get("document") or {}).get("title")
        if not title:
            raise RuntimeError(f"create_new_document named no document: {created}")
        try:
            build()
            volume = data(self.call("get_mass_properties"), "volume_mm3")
            size = data(self.call("get_bounding_box", {"exact": True}), "size_mm")
            self.report["recorded"][label] = {"volume_mm3": volume, "size_mm": size}
            return volume, size
        finally:
            self.report["steps"].append({f"close_{label}": self.call("close_document", {"title": title}).get("ok")})

    def polyline(self, points: list[tuple[float, float]]) -> None:
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            self.require("draw_line", {"x1_mm": x1, "y1_mm": y1, "x2_mm": x2, "y2_mm": y2})

    def thin_l(self, **thin: Any) -> Callable[[], None]:
        def build() -> None:
            self.require("create_sketch", {"plane": "front", "name": "L_Skizze"})
            self.polyline(L_POINTS)
            self.require("close_sketch")
            self.require("boss_extrude", {"depth_mm": 20, "sketch_name": "L_Skizze", **thin})
        return build


def run_cases(run: Run) -> None:
    checks = run.report["checks"]

    volume, size = run.part("l_mid", run.thin_l(thin_mm=5, thin_side="mid_plane"))
    checks["a_l_mid"] = close_to(volume, 10_000.0) and size_is(size, (62.5, 42.5, 20.0))

    one = run.part("l_one_side", run.thin_l(thin_mm=5, thin_side="one_side"))
    other = run.part("l_reverse", run.thin_l(thin_mm=5, thin_side="reverse"))
    outer, inner = (10_500.0, (65.0, 45.0, 20.0)), (9_500.0, (60.0, 40.0, 20.0))

    def matches(got: tuple[Any, Any], wanted: tuple[float, tuple[float, float, float]]) -> bool:
        return close_to(got[0], wanted[0]) and size_is(got[1], wanted[1])

    checks["b_l_sides"] = (matches(one, outer) and matches(other, inner)) or (
        matches(one, inner) and matches(other, outer))

    volume, _ = run.part("l_two_side", run.thin_l(thin_mm=5, thin_side="two_side", thin_mm_2=2))
    checks["c_l_two_side"] = close_to(volume, 721.0 * 20) or close_to(volume, 679.0 * 20)

    def sleeve(thin: bool) -> Callable[[], None]:
        def build() -> None:
            run.require("create_sketch", {"plane": "front", "name": "Huelse_Skizze"})
            run.require("draw_centerline", {"x1_mm": -10, "y1_mm": 0, "x2_mm": 60, "y2_mm": 0})
            if thin:
                run.require("draw_line", {"x1_mm": 0, "y1_mm": 20, "x2_mm": 50, "y2_mm": 20})
            else:
                run.polyline([(0, 18.5), (50, 18.5), (50, 21.5), (0, 21.5), (0, 18.5)])
            run.require("close_sketch")
            args: dict[str, Any] = {"angle_deg": 360, "sketch_name": "Huelse_Skizze"}
            if thin:
                args.update(thin_mm=3, thin_side="mid_plane")
            run.require("revolve", args)
        return build

    volume, size = run.part("sleeve_thin", sleeve(True))
    checks["d_sleeve_thin"] = close_to(volume, SLEEVE) and size_is(size, (50.0, 43.0, 43.0))
    volume, size = run.part("sleeve_solid", sleeve(False))
    checks["d_sleeve_solid"] = close_to(volume, SLEEVE) and size_is(size, (50.0, 43.0, 43.0))

    def thin_cut() -> None:
        run.require("create_sketch", {"plane": "front", "name": "Block_Skizze"})
        run.require("draw_rectangle", {"x1_mm": -30, "y1_mm": -20, "x2_mm": 30, "y2_mm": 20})
        run.require("close_sketch")
        run.require("boss_extrude", {"depth_mm": 20, "sketch_name": "Block_Skizze"})
        run.require("create_sketch", {"plane": "front", "name": "Nut_Skizze"})
        run.require("draw_circle", {"x_mm": 0, "y_mm": 0, "radius_mm": 10})
        run.require("close_sketch")
        run.require("cut_extrude", {"depth_mm": 5, "sketch_name": "Nut_Skizze", "thin_mm": 2,
                                    "thin_side": "mid_plane"})

    volume, size = run.part("thin_cut", thin_cut)
    checks["e_thin_cut"] = close_to(volume, 48_000.0 - 200.0 * math.pi) and size_is(size, (60.0, 40.0, 20.0))

    checks["f_answers"] = len(run.thin_answers) == 6 and all(
        a.get("ok") is True and data(a, "thin_mm") is not None and data(a, "thin_side") for a in run.thin_answers)


def main() -> int:
    run = Run()
    try:
        run_cases(run)
    except Exception as exc:  # noqa: BLE001 - the report must still be printed
        run.report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        print(json.dumps(run.report, indent=2, default=str))
    checks = run.report["checks"]
    return 0 if "error" not in run.report and set(checks) == set(CHECKS) and all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
