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

"""Live check of the weldment tools against closed-form geometry.

Run this only on a workstation with SOLIDWORKS already running and nothing
else driving it:

    ..\\.venv\\Scripts\\python.exe tests\\live_weldment.py

A 3D sketch of three connected lines (300, 200 and 150 mm) carries ISO
square tube 20 x 20 x 2 members.  The profile area A is read off a member's
end face (rounded to 1e-4 mm2, hence the tolerances); the mitred pair must then measure A * 500, the third member A * 150,
an end cap 3 mm thick on the open end measures 3 * 18 * 18 (inset half a
wall), and after trimming the third member flush to the second its box ends
10 mm lower and it measures A * 140.  Two gussets in the first corner measure
a*b*t/2 (triangle) and (a*b - cut corner)*t (polygon); the first member cut at
a reference plane falls into two pieces, the free one A * 150 (the other
keeps its mitre).  The part is closed without saving.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp.sw_core import active_document, running_app, value
from solidworks_mcp.sw_file import create_new_document
from solidworks_mcp.sw_inspect import list_bodies, list_faces
from solidworks_mcp.sw_refgeom import create_plane
from solidworks_mcp.sw_weldment import (
    create_3d_sketch,
    list_weldment_profiles,
    weldment_end_cap,
    weldment_gusset,
    weldment_structural_member,
    weldment_trim_extend,
)

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


def face_where(normal: list[float], predicate: Any) -> dict[str, Any]:
    for face in require(list_faces({"surface_type": "plane"}), "list_faces")["data"]["faces"]:
        n = face.get("normal")
        if n and all(abs(n[i] - normal[i]) < 1e-3 for i in range(3)) and predicate(face["point_mm"]):
            return face
    raise SystemExit(f"no planar face with normal {normal}")


def body_index(fragment: str) -> int:
    for body in require(list_bodies({}), "list_bodies")["data"]["bodies"]:
        if fragment in body["name"]:
            return int(body["index"])
    raise SystemExit(f"no body named like {fragment}")


def main() -> int:
    require(create_new_document({"kind": "part"}), "create_new_document")
    _, doc = active_document()
    title = str(value(doc, "GetTitle"))
    try:
        sketch = require(create_3d_sketch({"lines": [
            {"x1_mm": 0, "y1_mm": 0, "z1_mm": 0, "x2_mm": 300, "y2_mm": 0, "z2_mm": 0},
            {"x1_mm": 300, "y1_mm": 0, "z1_mm": 0, "x2_mm": 300, "y2_mm": 200, "z2_mm": 0},
            {"x1_mm": 300, "y1_mm": 200, "z1_mm": 0, "x2_mm": 300, "y2_mm": 200, "z2_mm": -150},
        ]}), "create_3d_sketch")
        check("3D sketch segments", sketch["data"]["segments"], [0, 1, 2])

        profiles = require(list_weldment_profiles({"standard": "iso", "type": "square tube"}), "list_weldment_profiles")
        check("iso square tube 20 x 20 x 2 listed", any(p["size"] == "20 x 20 x 2" for p in profiles["data"]["profiles"]), True)

        pair = require(weldment_structural_member({
            "selection": {"sketch_segments": [0, 1], "sketch_name": sketch["data"]["sketch"]},
            "standard": "iso", "type": "square tube", "size": "20 x 20 x 2",
        }), "weldment_structural_member(pair)")
        check("weldment feature added", pair["data"]["weldment_added"], True)
        check("two bodies from two segments", len(pair["data"]["bodies"]), 2)
        end = face_where([-1, 0, 0], lambda p: abs(p[0]) < 1e-3)
        area = float(end["area_mm2"])
        check("profile area is a 20 x 20 x 2 tube with rounded corners", 120 < area < 144, True)
        check("mitred pair volume = A * (300 + 200)", pair["data"]["volume_mm3"], area * 500, 1.0)

        third = require(weldment_structural_member({
            "selection": {"sketch_segments": [2], "sketch_name": sketch["data"]["sketch"]},
            "standard": "iso", "type": "square tube", "size": "20 x 20 x 2",
        }), "weldment_structural_member(third)")
        check("third member volume = A * 150", third["data"]["volume_mm3"], area * 150, 0.3)
        check("third member box", third["data"]["bodies"][0]["size_mm"], [20.0, 20.0, 150.0])

        end = face_where([-1, 0, 0], lambda p: abs(p[0]) < 1e-3)
        cap = require(weldment_end_cap({"selection": {"faces": [int(end["index"])]}, "thickness_mm": 3}), "weldment_end_cap")
        check("end cap volume = 3 * 18 * 18", cap["data"]["volume_mm3"], 3 * 18 * 18)
        check("end cap thickness readback", cap["data"]["end_cap"]["thickness_mm"], 3.0, 1e-6)
        check("end cap sits beyond the end", cap["data"]["bodies"][0]["min_mm"][0], -3.0)

        # Gussets in the inner corner between the first two members: the top
        # face of member 1 (y = 10) and the inner face of member 2 (x = 290,
        # centred at y = 105).  The third member's inner face also lies at
        # x = 290 (centred at y = 200); which of the two list_faces reports
        # first varies between runs, so the predicate names member 2.
        legs = [face_where([0, 1, 0], lambda p: abs(p[1] - 10) < 1e-3 and p[0] < 290),
                face_where([-1, 0, 0], lambda p: abs(p[0] - 290) < 1e-3 and p[1] < 190)]
        faces = [int(f["index"]) for f in legs]
        tri = require(weldment_gusset({"selection": {"faces": faces}, "d1_mm": 50, "d2_mm": 30, "thickness_mm": 5}), "weldment_gusset(triangle)")
        check("triangle gusset volume = 50 * 30 * 5 / 2", tri["data"]["volume_mm3"], 50 * 30 * 5 / 2)
        check("triangle gusset centred on the corner edge", tri["data"]["bodies"][0]["min_mm"][2], -2.5)
        legs = [face_where([0, 1, 0], lambda p: abs(p[1] - 10) < 1e-3 and p[0] < 240),
                face_where([-1, 0, 0], lambda p: abs(p[0] - 290) < 1e-3 and 60 < p[1] < 190)]
        poly = require(weldment_gusset({
            "selection": {"faces": [int(f["index"]) for f in legs]}, "profile": "polygon",
            "d1_mm": 50, "d2_mm": 50, "d3_mm": 20, "d4_mm": 20, "thickness_mm": 4, "thickness_direction": "outer",
        }), "weldment_gusset(polygon)")
        check("polygon gusset volume = (50 * 50 - 30 * 30 / 2) * 4", poly["data"]["volume_mm3"], (2500 - 450) * 4)
        check("polygon gusset readback", poly["data"]["gusset"]["profile"], "polygon")

        trimmed = require(weldment_trim_extend({
            "bodies": [body_index("(2)")], "trimming_bodies": [body_index("(1)[2]")], "corner_type": "butt1",
        }), "weldment_trim_extend")
        grown = [b for b in trimmed["data"]["trimmed_bodies"] if abs(b["size_mm"][1] - 220) < 0.01]
        check("the trimming member grows over the trimmed end (200 -> 210)", len(grown), 1)
        third_after = [b for b in trimmed["data"]["trimmed_bodies"] if abs(b["size_mm"][2] - 140) < 1]
        check("third member trimmed flush to the second", bool(third_after), True)
        if third_after:
            check("trimmed member top at z = -10", third_after[0]["max_mm"][2], -10.0)
            check("trimmed member volume = A * 140", third_after[0]["volume_mm3"], area * 140, 0.3)

        # Trimming against a reference plane: the first member, 300 mm along
        # x, cut at a plane 150 mm from the right plane (x = 0) keeps 150 mm.
        plane = require(create_plane({"mode": "offset", "selection": {"planes": ["right"]}, "distance_mm": 150, "name": "Schnitt"}), "create_plane")
        butt = weldment_trim_extend({
            "bodies": [body_index("(1)[1]")], "trimming_selection": {"planes": [plane["data"]["feature"]]}, "corner_type": "butt1",
        })
        check("butt against a plane is reported as trimming nothing", butt["ok"], False)
        plane_trim = require(weldment_trim_extend({
            "bodies": [body_index("(1)[1]")], "trimming_selection": {"planes": [plane["data"]["feature"]]},
        }), "weldment_trim_extend(plane)")
        check("plane boundary defaults to corner_type trim", plane_trim["data"]["corner_type"], "trim")
        pieces = plane_trim["data"]["trimmed_bodies"]
        check("member cut at the plane into two pieces", len(pieces), 2)
        left = [b for b in pieces if abs(b["size_mm"][0] - 150) < 0.01]
        check("the free piece is 150 mm long", len(left), 1)
        if left:
            check("free piece volume = A * 150", left[0]["volume_mm3"], area * 150, 0.3)
    finally:
        running_app().CloseDoc(str(value(doc, "GetTitle") or title))

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    for label, _, detail in failed:
        print(f"  FAIL {label}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
