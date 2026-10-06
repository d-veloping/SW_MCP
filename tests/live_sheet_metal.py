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

"""Live check of the sheet metal tools against closed-form geometry.

Run this only on a workstation with SOLIDWORKS already running and nothing
else driving it:

    ..\\.venv\\Scripts\\python.exe tests\\live_sheet_metal.py

Two parts are built from scratch and every feature is checked by a number
SOLIDWORKS cannot fake: the volume a flange adds, the developed length of
the flat pattern, the bend lines in the exported DXF.

Part A, an L profile: base flange from an open sketch, flat length against
the K-factor formula, a closed hem, a miter flange on the free leg, DXF export
with three bend lines.

Part B, a plate: two edge flanges, a closed corner with its gap read back, a
break corner (fillet) that removes exactly (1 - pi/4) r^2 t, sheet_metal_info,
and DXF/DWG exports with and without bend lines.

The parts are saved under <output root>/live_sheet_metal and closed at the
end; nothing already open is touched.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp.sw_core import OUTPUT_ROOT, active_document, running_app, value
from solidworks_mcp.sw_file import create_new_document, save_document
from solidworks_mcp.sw_inspect import get_mass_properties, list_edges, list_faces
from solidworks_mcp.sw_sheetmetal import (
    export_flat_pattern,
    sheet_metal_base_flange,
    sheet_metal_break_corner,
    sheet_metal_closed_corner,
    sheet_metal_edge_flange,
    sheet_metal_flatten,
    sheet_metal_hem,
    sheet_metal_info,
    sheet_metal_miter_flange,
)
from solidworks_mcp.sw_sketch import close_sketch, create_sketch, draw_line, draw_rectangle

FOLDER = "live_sheet_metal"
CHECKS: list[tuple[str, bool, Any]] = []


def require(payload: dict[str, Any], context: str) -> dict[str, Any]:
    if not payload.get("ok"):
        raise SystemExit(f"{context} failed: {json.dumps(payload, default=str)[:600]}")
    return payload


def check(label: str, got: Any, expected: Any, tolerance: float = 0.01) -> None:
    if isinstance(expected, (int, float)) and isinstance(got, (int, float)):
        passed = abs(float(got) - float(expected)) <= tolerance
    else:
        passed = got == expected
    CHECKS.append((label, passed, {"got": got, "expected": expected}))
    print(f"  [{'ok' if passed else 'FAIL'}] {label}: {got} (expected {expected})")


def volume() -> float:
    return float(require(get_mass_properties({}), "get_mass_properties")["data"]["volume_mm3"])


def edge_at(point: list[float]) -> int:
    for edge in require(list_edges({}), "list_edges")["data"]["edges"]:
        mid = edge.get("point_mm")
        if mid and all(abs(mid[i] - point[i]) < 1e-3 for i in range(3)):
            return int(edge["index"])
    raise SystemExit(f"no edge with midpoint {point}")


def face_where(normal: list[float], area: float, predicate: Any) -> int:
    for face in require(list_faces({}), "list_faces")["data"]["faces"]:
        n = face.get("normal")
        if n is None or abs(face.get("area_mm2", -1) - area) > 0.01:
            continue
        if all(abs(n[i] - normal[i]) < 1e-3 for i in range(3)) and predicate(face["point_mm"]):
            return int(face["index"])
    raise SystemExit(f"no face with normal {normal} and area {area}")


def bend_allowance(radius: float, thickness: float, k: float, angle_deg: float = 90) -> float:
    return math.radians(angle_deg) * (radius + k * thickness)


def part_a() -> None:
    print("Part A: L profile")
    require(create_new_document({"kind": "part"}), "create_new_document")
    _, doc = active_document()
    title = str(value(doc, "GetTitle"))
    try:
        require(create_sketch({"plane": "front"}), "create_sketch")
        require(draw_line({"x1_mm": 0, "y1_mm": 0, "x2_mm": 50, "y2_mm": 0}), "draw_line")
        require(draw_line({"x1_mm": 50, "y1_mm": 0, "x2_mm": 50, "y2_mm": 30}), "draw_line")
        require(close_sketch({}), "close_sketch")
        base = require(sheet_metal_base_flange({"thickness_mm": 2, "bend_radius_mm": 1, "depth_mm": 40}), "sheet_metal_base_flange")
        t, r, w = 2.0, 1.0, 40.0
        expected = w * (t * (50 - r) + t * (30 - r) + math.pi / 4 * ((r + t) ** 2 - r ** 2))
        check("base flange volume", base["data"]["volume_mm3"], expected)
        check("base flange size", base["data"]["size_mm"], [52.0, 32.0, 40.0])
        k = float(base["data"]["sheet_metal"]["k_factor"])
        check("sheet metal thickness readback", base["data"]["sheet_metal"]["thickness_mm"], 2.0, 1e-6)

        flat = require(sheet_metal_flatten({"flat": True}), "sheet_metal_flatten")
        check("flat pattern length", flat["data"]["size_mm"][0], (50 - r) + (30 - r) + bend_allowance(r, t, k))
        check("flat pattern thickness", flat["data"]["size_mm"][1], 2.0)
        check("bend state flattened", flat["data"]["bend_state"], "flattened")
        require(sheet_metal_flatten({"flat": False}), "sheet_metal_flatten(fold)")

        before = volume()
        hem = require(sheet_metal_hem({"selection": {"edges": [edge_at([0, -2, 20])]}, "type": "closed", "length_mm": 10}), "sheet_metal_hem")
        check("hem length readback", hem["data"]["hem"]["length_mm"], 10.0, 1e-6)
        check("hem adds material", hem["data"]["volume_mm3"] > before, True)

        # Miter profile on the front plane, which is perpendicular to the free
        # top edge of the second leg and contains its end point; the line
        # starts on the outer edge and leaves the leg at a right angle.
        require(create_sketch({"plane": "front"}), "create_sketch(front)")
        require(draw_line({"x1_mm": 52, "y1_mm": 30, "x2_mm": 67, "y2_mm": 30}), "draw_line(miter)")
        require(close_sketch({}), "close_sketch")
        before = volume()
        miter = require(sheet_metal_miter_flange({"selection": {"edges": [edge_at([52, 30, 20])]}, "gap_mm": 0.5}), "sheet_metal_miter_flange")
        arc = math.pi / 4 * ((r + t) ** 2 - r ** 2)
        check("miter flange volume", miter["data"]["volume_mm3"] - before, w * (t * (15 - r - t) + arc - t * r))
        check("miter flange reach", miter["data"]["size_mm"][0], 67.0)

        require(save_document({"path": f"{FOLDER}/l_profile.sldprt", "overwrite": True}), "save_document")
        exported = require(export_flat_pattern({"path": f"{FOLDER}/l_profile.dxf", "overwrite": True}), "export_flat_pattern")
        check("dxf bend lines (base bend + hem + miter)", exported["data"]["dxf"]["bend_lines"], 3)
        check("dxf width", exported["data"]["dxf"]["extents_mm"][0], 40.0)
    finally:
        # Saving renames the window, so the title is read again before closing.
        running_app().CloseDoc(str(value(doc, "GetTitle") or title))


def part_b() -> None:
    print("Part B: plate with flanges")
    require(create_new_document({"kind": "part"}), "create_new_document")
    _, doc = active_document()
    title = str(value(doc, "GetTitle"))
    try:
        require(create_sketch({"plane": "top"}), "create_sketch")
        require(draw_rectangle({"x1_mm": 0, "y1_mm": 0, "x2_mm": 60, "y2_mm": 40}), "draw_rectangle")
        require(close_sketch({}), "close_sketch")
        base = require(sheet_metal_base_flange({"thickness_mm": 2, "bend_radius_mm": 1}), "sheet_metal_base_flange")
        check("plate volume", base["data"]["volume_mm3"], 4800.0)
        t, r = 2.0, 1.0
        arc = math.pi / 4 * ((r + t) ** 2 - r ** 2)

        before = volume()
        flange1 = require(sheet_metal_edge_flange({"selection": {"edges": [edge_at([30, 0, 0])]}, "length_mm": 20}), "sheet_metal_edge_flange 1")
        check("edge flange 1 volume", flange1["data"]["volume_mm3"] - before, 60 * (t * (20 - r) + arc))
        check("edge flange 1 height", flange1["data"]["size_mm"][1], 22.0)

        before = volume()
        flange2 = require(sheet_metal_edge_flange({"selection": {"edges": [edge_at([60, 0, -20])]}, "length_mm": 20}), "sheet_metal_edge_flange 2")
        check("edge flange 2 volume", flange2["data"]["volume_mm3"] - before, 40 * (t * (20 - r) + arc))

        face = face_where([1, 0, 0], 38.0, lambda p: abs(p[0] - 60) < 1e-3 and 1 <= p[1] <= 20)
        corner = require(sheet_metal_closed_corner({"selection": {"faces": [face]}, "corner_type": "butt", "gap_mm": 0.5}), "sheet_metal_closed_corner")
        check("closed corner gap readback", corner["data"]["corner"]["gap_mm"], 0.5, 1e-6)
        check("closed corner type readback", corner["data"]["corner"]["corner_type"], "butt")
        check("closed corner adds material", corner["data"]["volume_mm3"] > flange2["data"]["volume_mm3"], True)

        before = volume()
        broken = require(sheet_metal_break_corner({"selection": {"edges": [edge_at([0, 20, 2])]}, "mode": "fillet", "distance_mm": 3}), "sheet_metal_break_corner")
        check("break corner removes (1-pi/4) r^2 t", before - broken["data"]["volume_mm3"], (1 - math.pi / 4) * 9 * t)

        info = require(sheet_metal_info({}), "sheet_metal_info")
        kinds = sorted({f["kind"] for f in info["data"]["features"]})
        check("info lists the features", kinds, ["base_flange", "break_corner", "closed_corner", "edge_flange", "flat_pattern", "sheet_metal"])
        check("info thickness", info["data"]["parameters"]["thickness_mm"], 2.0, 1e-6)

        flat = require(sheet_metal_flatten({"flat": True}), "sheet_metal_flatten")
        check("plate flattens to one thickness", flat["data"]["size_mm"][1], 2.0)
        require(sheet_metal_flatten({"flat": False}), "sheet_metal_flatten(fold)")

        unsaved = export_flat_pattern({"path": f"{FOLDER}/plate_unsaved.dxf", "overwrite": True})
        check("export refuses an unsaved part", unsaved["ok"], False)
        require(save_document({"path": f"{FOLDER}/plate.sldprt", "overwrite": True}), "save_document")
        with_bends = require(export_flat_pattern({"path": f"{FOLDER}/plate.dxf", "overwrite": True}), "export_flat_pattern")
        check("dxf bend lines (two flanges)", with_bends["data"]["dxf"]["bend_lines"], 2)
        without = require(export_flat_pattern({"path": f"{FOLDER}/plate_outline.dxf", "overwrite": True, "bend_lines": False}), "export_flat_pattern(no bends)")
        check("dxf without bend lines", without["data"]["dxf"]["bend_lines"], 0)
        check("dxf outline unchanged", without["data"]["dxf"]["outline_entities"], with_bends["data"]["dxf"]["outline_entities"])
        dwg = require(export_flat_pattern({"path": f"{FOLDER}/plate.dwg", "overwrite": True}), "export_flat_pattern(dwg)")
        check("dwg written", (OUTPUT_ROOT / FOLDER / "plate.dwg").is_file() and dwg["data"]["bytes"] > 0, True)
    finally:
        # Saving renames the window, so the title is read again before closing.
        running_app().CloseDoc(str(value(doc, "GetTitle") or title))


def main() -> int:
    part_a()
    part_b()
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    for label, _, detail in failed:
        print(f"  FAIL {label}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
