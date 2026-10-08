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

Three parts are built from scratch. The flanges, the flat pattern, the break
corner and the DXF are checked by a number SOLIDWORKS cannot fake: the volume
a flange adds, the developed length of the flat pattern, the material a break
corner removes, the bend lines in the exported DXF; the hem, the closed corner
and the corner relief by added or removed material plus their readback.

Part A, an L profile: base flange from an open sketch, flat length against
the K-factor formula, a closed hem, a miter flange on the free leg, DXF export
with three bend lines.

Part B, a plate: three edge flanges, a square corner relief at one corner,
a closed corner with its gap read back at the other, a break corner (fillet) that removes exactly (1 - pi/4) r^2 t, sheet_metal_info,
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
    sheet_metal_corner_relief,
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

        before = volume()
        flange3 = require(sheet_metal_edge_flange({"selection": {"edges": [edge_at([0, 0, -20])]}, "length_mm": 20}), "sheet_metal_edge_flange 3")
        check("edge flange 3 volume", flange3["data"]["volume_mm3"] - before, 40 * (t * (20 - r) + arc))

        # The corner between flange 1 (bend along x at z = 0) and flange 3
        # (bend along z at x = 0): their outer bend faces lie at x < 35.
        bends = [f["index"] for f in require(list_faces({"surface_type": "cylinder"}), "list_faces")["data"]["faces"]
                 if abs(f["radius_mm"] - 3.0) < 1e-6 and f["point_mm"][0] < 35]
        check("two outer bend faces at the x = 0 corner", len(bends), 2)
        relief = require(sheet_metal_corner_relief({"corners": [{"faces": bends}], "relief_type": "square", "size_mm": 3}), "sheet_metal_corner_relief")
        check("corner relief removes material", relief["data"]["removed_mm3"] > 0, True)
        check("corner relief accepted one corner", relief["data"]["corners_accepted"], 1)

        face = face_where([1, 0, 0], 38.0, lambda p: abs(p[0] - 60) < 1e-3 and 1 <= p[1] <= 20)
        corner = require(sheet_metal_closed_corner({"selection": {"faces": [face]}, "corner_type": "butt", "gap_mm": 0.5}), "sheet_metal_closed_corner")
        check("closed corner gap readback", corner["data"]["corner"]["gap_mm"], 0.5, 1e-6)
        check("closed corner type readback", corner["data"]["corner"]["corner_type"], "butt")
        check("closed corner adds material", corner["data"]["volume_mm3"] > relief["data"]["volume_mm3"], True)

        before = volume()
        broken = require(sheet_metal_break_corner({"selection": {"edges": [edge_at([0, 20, 2])]}, "mode": "fillet", "distance_mm": 3}), "sheet_metal_break_corner")
        check("break corner removes (1-pi/4) r^2 t", before - broken["data"]["volume_mm3"], (1 - math.pi / 4) * 9 * t)

        info = require(sheet_metal_info({}), "sheet_metal_info")
        kinds = sorted({f["kind"] for f in info["data"]["features"]})
        check("info lists the features", kinds, ["base_flange", "break_corner", "closed_corner", "corner_relief", "edge_flange", "flat_pattern", "sheet_metal"])
        check("info thickness", info["data"]["parameters"]["thickness_mm"], 2.0, 1e-6)

        flat = require(sheet_metal_flatten({"flat": True}), "sheet_metal_flatten")
        check("plate flattens to one thickness", flat["data"]["size_mm"][1], 2.0)
        require(sheet_metal_flatten({"flat": False}), "sheet_metal_flatten(fold)")

        unsaved = export_flat_pattern({"path": f"{FOLDER}/plate_unsaved.dxf", "overwrite": True})
        check("export refuses an unsaved part", unsaved["ok"], False)
        require(save_document({"path": f"{FOLDER}/plate.sldprt", "overwrite": True}), "save_document")
        with_bends = require(export_flat_pattern({"path": f"{FOLDER}/plate.dxf", "overwrite": True}), "export_flat_pattern")
        check("dxf bend lines (three flanges)", with_bends["data"]["dxf"]["bend_lines"], 3)
        without = require(export_flat_pattern({"path": f"{FOLDER}/plate_outline.dxf", "overwrite": True, "bend_lines": False}), "export_flat_pattern(no bends)")
        check("dxf without bend lines", without["data"]["dxf"]["bend_lines"], 0)
        check("dxf outline unchanged", without["data"]["dxf"]["outline_entities"], with_bends["data"]["dxf"]["outline_entities"])
        dwg = require(export_flat_pattern({"path": f"{FOLDER}/plate.dwg", "overwrite": True}), "export_flat_pattern(dwg)")
        check("dwg written", (OUTPUT_ROOT / FOLDER / "plate.dwg").is_file() and dwg["data"]["bytes"] > 0, True)
    finally:
        # Saving renames the window, so the title is read again before closing.
        running_app().CloseDoc(str(value(doc, "GetTitle") or title))


def part_c() -> None:
    """The option surface: an edge flange and a miter flange with their readable options off the defaults.

    flip and trim_side_bends have no readback and stay at their defaults here;
    the relief type of the edge flange has none either and is only passed.
    """
    print("Part C: options")
    require(create_new_document({"kind": "part"}), "create_new_document")
    _, doc = active_document()
    title = str(value(doc, "GetTitle"))
    try:
        require(create_sketch({"plane": "front"}), "create_sketch")
        require(draw_line({"x1_mm": 0, "y1_mm": 0, "x2_mm": 50, "y2_mm": 0}), "draw_line")
        require(draw_line({"x1_mm": 50, "y1_mm": 0, "x2_mm": 50, "y2_mm": 30}), "draw_line")
        require(close_sketch({}), "close_sketch")
        require(sheet_metal_base_flange({"thickness_mm": 2, "bend_radius_mm": 1, "depth_mm": 40}), "sheet_metal_base_flange")
        flange = require(sheet_metal_edge_flange({
            "selection": {"edges": [edge_at([0, 0, 20])]}, "length_mm": 12, "angle_deg": 60, "bend_radius_mm": 2,
            "position": "material_inside", "relief_type": "obround", "relief_ratio": 0.4,
        }), "sheet_metal_edge_flange(options)")
        applied = flange["data"]["flange"]
        check("edge flange angle read back", applied["angle_deg"], 60.0, 1e-6)
        check("edge flange radius read back", applied["bend_radius_mm"], 2.0, 1e-6)
        check("edge flange position read back", applied["position"], "material_inside")
        check("edge flange length read back from the inner virtual sharp", applied["length_mm"], 12.0, 1e-6)
        check("edge flange relief ratio read back", applied["relief_ratio"], 0.4, 1e-6)
        # A 60-degree flange of 12 mm from the inner sharp, material inside, radius 2, on a 2 mm sheet
        # reaches 57.42265 mm in x (measured 2026-10-08); the default bend_outside reaches 60.309401.
        check("edge flange reach with material inside", flange["data"]["size_mm"][0], 57.42265)

        require(create_sketch({"plane": "front"}), "create_sketch(front)")
        require(draw_line({"x1_mm": 52, "y1_mm": 30, "x2_mm": 67, "y2_mm": 30}), "draw_line(miter)")
        require(close_sketch({}), "close_sketch")
        miter = require(sheet_metal_miter_flange({
            "selection": {"edges": [edge_at([52, 30, 20])]}, "bend_radius_mm": 2, "gap_mm": 1, "position": "material_outside",
            "trim_side_bends": False, "start_offset_mm": 5, "end_offset_mm": 3, "relief_type": "obround", "relief_ratio": 0.6,
        }), "sheet_metal_miter_flange(options)")
        applied = miter["data"]["flange"]
        check("miter flange radius, gap, offsets read back", [applied["bend_radius_mm"], applied["gap_mm"], applied["start_offset_mm"], applied["end_offset_mm"]], [2.0, 1.0, 5.0, 3.0])
        check("miter flange position and relief read back", [applied["position"], applied["relief_type"], applied["relief_ratio"]], ["material_outside", "obround", 0.6])
        # The offsets show in the geometry: the miter's end faces sit at z = 5 and
        # z = 37, and its top face is (40 - 5 - 3) wide by 13 long (15 minus the
        # bend radius, with the material outside the edge).
        miter_faces = [f for f in require(list_faces({}), "list_faces")["data"]["faces"] if f["point_mm"][0] > 52.5]
        ends = sorted(round(f["point_mm"][2], 3) for f in miter_faces if f.get("normal") and abs(abs(f["normal"][2]) - 1) < 1e-6)
        check("miter flange end faces at the offsets z = 5 and z = 37", ends, [5.0, 37.0])
        top = [f for f in miter_faces if f.get("normal") and abs(f["normal"][1] - 1) < 1e-6]
        check("miter flange top face is 32 x 13", top[0]["area_mm2"] if top else None, 32.0 * 13.0)
        # The part box: the miter reaches x = 67, the edge flange 5.42265 beyond x = 0.
        check("miter flange reach 67 plus the edge flange beyond x = 0", miter["data"]["size_mm"][0], 67.0 + (57.42265 - 52.0))
    finally:
        running_app().CloseDoc(str(value(doc, "GetTitle") or title))


def main() -> int:
    part_a()
    part_b()
    part_c()
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    for label, _, detail in failed:
        print(f"  FAIL {label}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
