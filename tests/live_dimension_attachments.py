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

"""Live check: check_dimension_attachments reads the hidden edges of a bore and judges the dimensions on the sheet.

A new part gets a block 40 x 20 x 30 mm (front plane, extruded 30) and a blind bore of radius 4 mm and depth 12 mm
from the front face; it is saved to a temporary folder under the server's output root. A drawing gets the front view
and two projections off it, to the right without and below with hidden lines, then the model's dimensions, and every
view is checked. The script checks:

(a) the part has the volume of block minus bore (geometry, not the tools' answers);
(b) every view reads the 14 edges of the part (12 of the block, the 2 circles of the bore) and shows fewer, since the
    bore is hidden in all three;
(c) the block depth (D1 of the boss) is listed in at least one view, with readable points, and never points into the
    void;
(d) every view reads back in the display mode it was placed with after its check (the one below with hidden lines
    visible, so the check really sets it back), and no call failed;
(e) both projections off the front view exist as separate views (the second one needs the parent selected).

A dimension that does point into the void is not part of this check: SOLIDWORKS 2016 imports no model dimension onto a
hidden edge (here it leaves out the bore depth altogether), so the positive case is covered offline only.

Run this only on a workstation with SOLIDWORKS already running and nothing else driving it:

    ..\\.venv\\Scripts\\python.exe tests\\live_dimension_attachments.py

Drawing and part are closed without saving and the temporary folder is removed. The output is one JSON object; exit
code 0 when all checks hold.
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp.sw_core import OUTPUT_ROOT
from solidworks_mcp.sw_drawing import (check_dimension_attachments, create_drawing, insert_model_annotations,
                                       insert_model_view, insert_projected_view, list_drawing_views)
from solidworks_mcp.sw_feature import boss_extrude, cut_extrude
from solidworks_mcp.sw_file import (close_document, create_new_document, list_open_documents, open_document,
                                    save_document)
from solidworks_mcp.sw_inspect import get_mass_properties
from solidworks_mcp.sw_sketch import close_sketch, create_sketch, draw_circle, draw_rectangle

VOLUME_MM3 = 40 * 20 * 30 - math.pi * 4 ** 2 * 12


def open_titles() -> list[str]:
    return [d["title"] for d in (list_open_documents({}).get("data") or {}).get("documents") or []]


def main() -> int:
    report: dict[str, Any] = {"steps": {}, "checks": {}}
    before = set(open_titles())
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="live_attachments_", dir=OUTPUT_ROOT))
    failed: list[str] = []

    def step(name: str, call: Any, args: dict[str, Any]) -> dict[str, Any]:
        answer = call(args)
        report["steps"][name] = {"ok": bool(answer.get("ok")), "message": answer.get("message")}
        if not answer.get("ok"):
            failed.append(name)
        return answer

    try:
        for name, call, args in (
            ("create_new_document", create_new_document, {"kind": "part"}),
            ("create_sketch", create_sketch, {"plane": "front"}),
            ("draw_rectangle", draw_rectangle, {"x1_mm": 0, "y1_mm": 0, "x2_mm": 40, "y2_mm": 20}),
            ("close_sketch", close_sketch, {}),
            ("boss_extrude", boss_extrude, {"depth_mm": 30}),
            ("create_sketch_bore", create_sketch, {"plane": "front"}),
            ("draw_circle", draw_circle, {"x_mm": 20, "y_mm": 10, "radius_mm": 4}),
            ("close_sketch_bore", close_sketch, {}),
            ("cut_extrude", cut_extrude, {"depth_mm": 12}),
            ("save_document", save_document, {"path": str(folder / "attachments.sldprt"), "overwrite": True}),
        ):
            if failed:
                break
            step(name, call, args)
        volume = (get_mass_properties({}).get("data") or {}).get("volume_mm3")
        report["volume_mm3"] = volume
        report["checks"]["a_volume"] = isinstance(volume, (int, float)) and abs(volume - VOLUME_MM3) < 1.0
        if failed or not report["checks"]["a_volume"]:
            return 1
        # a part created in this run does not stay out of the way of a new drawing; a reopened one does
        for name, call, args in (
            ("close_part", close_document, {"title": "attachments"}),
            ("open_document", open_document, {"path": str(folder / "attachments.sldprt")}),
            ("create_drawing", create_drawing, {"paper_size": "A3", "first_angle": True}),
        ):
            step(name, call, args)
            if failed:
                return 1
        front = step("insert_model_view", insert_model_view,
                     {"view": "front", "x_mm": 100, "y_mm": 200, "model_path": str(folder / "attachments.sldprt"),
                      "scale": 1.0, "display_mode": "hidden_lines_removed"})
        parent = ((front.get("data") or {}).get("view") or {}).get("name")
        if failed or not parent:
            return 1
        placed = {parent: "hidden_lines_removed"}
        projected = []
        for direction, mode in (("right", "hidden_lines_removed"), ("down", "hidden_lines_visible")):
            answer = step(f"insert_projected_view {direction}", insert_projected_view,
                          {"parent_view": parent, "direction": direction, "offset_mm": 90, "display_mode": mode})
            projected.append(((answer.get("data") or {}).get("view") or {}).get("name"))
            if failed:
                return 1
            placed[projected[-1]] = mode
        step("insert_model_annotations", insert_model_annotations, {"all_views": True})
        views = [v["name"] for v in (list_drawing_views({}).get("data") or {}).get("views") or []
                 if v.get("type") != "sheet"]
        report["projected"] = projected
        report["views"] = {}
        block = []
        for name in views:
            answer = step(f"check {name}", check_dimension_attachments, {"view_name": name})
            data = answer.get("data") or {}
            report["views"][name] = {"display_mode": data.get("display_mode"), "edges": data.get("edges"),
                                     "dimensions": [{k: d.get(k) for k in ("full_name", "into_void", "hidden_points")}
                                                    for d in data.get("dimensions") or []]}
            block += [d for d in data.get("dimensions") or [] if str(d.get("full_name", "")).startswith("D1@Boss-Extrude")]
        report["checks"].update(
            b_hidden_edges=bool(views) and all((v["edges"] or {}).get("all") == 14
                                               and (v["edges"] or {}).get("shown", 14) < 14
                                               and (v["edges"] or {}).get("unreadable") == 0
                                               and (v["edges"] or {}).get("unreadable_shown") == 0
                                               for v in report["views"].values()),
            c_block_not=bool(block) and all(d["into_void"] is False and d["hidden_points"] == [False, False]
                                            for d in block),
            d_modes_back=bool(views) and all(v["display_mode"] == placed.get(name)
                                              for name, v in report["views"].items()) and not failed,
            e_two_projections=bool(parent) and all(projected) and len(set(projected)) == 2
            and set(projected) <= set(views) and len(views) == 3,
        )
    finally:
        for title in sorted(set(open_titles()) - before, key=lambda t: "Draw" not in t and " - " not in t):
            # the drawing first, then the part it references
            report["steps"][f"close {title}"] = close_document({"title": title}).get("ok")
        report["left_open"] = sorted(set(open_titles()) - before)
        shutil.rmtree(folder, ignore_errors=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["checks"] and all(report["checks"].values()) and not report["left_open"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
