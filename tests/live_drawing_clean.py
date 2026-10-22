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

"""Live check: real section views and dimension clean-up (issue #2, d-veloping/ClauSW#157).

Run this only on a workstation with SOLIDWORKS already running and nothing
else driving it, with a saved turned part whose axis is the model x axis (the
ClauSW brake disc or shaft):

    ..\\.venv\\Scripts\\python.exe tests\\live_drawing_clean.py C:\\path\\part.sldprt

Steps: the exact box and the faces are read on the part; an A3 drawing gets
the circular view (left) and a section through the box centre; a second
section with its line beside the part must fail and leave neither view nor cutting line behind;
model dimensions go into every view, the duplicates are hidden in one call
(the section keeps its own), one view is arranged, a detail view is placed, a reversed section is placed and deleted with its line, a label in use is refused,
and the drawing is saved as <output root>/live_drawing_clean/section_<part>.pdf to
look at; a second sheet gets a view with dimensions, which must be listed too, and
with that sheet active a dimension of the first sheet is hidden and its section arranged. The drawing is closed without saving; the part is closed again only
when this run opened it, and must not be dirty.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp.sw_core import OUTPUT_ROOT, active_document, as_list, running_app, value
from solidworks_mcp.sw_drawing import (
    add_sheet,
    arrange_drawing_dimensions,
    create_drawing,
    delete_drawing_view,
    hide_drawing_dimensions,
    insert_detail_view,
    insert_model_annotations,
    insert_model_view,
    insert_section_view,
    list_drawing_dimensions,
    list_drawing_views,
)
from solidworks_mcp.sw_file import _save_as, open_document
from solidworks_mcp.sw_inspect import get_bounding_box, list_faces


def require(payload: dict[str, Any], context: str) -> dict[str, Any]:
    if not payload.get("ok"):
        raise SystemExit(f"{context} failed: {payload.get('message')}")
    return payload


def part_dirty(path: str) -> bool | None:
    for doc in as_list(value(running_app(), "GetDocuments")):
        if str(value(doc, "GetPathName") or "").lower() == path.lower():
            return bool(value(doc, "GetSaveFlag"))
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("part", help="a saved turned .sldprt with its axis along model x")
    args = parser.parse_args()
    part = str(Path(args.part).resolve())
    report: dict[str, Any] = {}

    opened = require(open_document({"path": part}), "open_document")
    own_part = opened["data"].get("already_open") is False
    box = require(get_bounding_box({"exact": True}), "get_bounding_box")["data"]
    centre = [round((a + b) / 2, 6) for a, b in zip(box["min_mm"], box["max_mm"])]
    faces = require(list_faces({"surface_type": "cylinder"}), "list_faces")["data"]["faces"]
    report["box_mm"] = box["size_mm"]
    report["cylinders_with_axis_point"] = sum(1 for f in faces if "axis_point_mm" in f)
    require(create_drawing({"paper_size": "A3", "first_angle": True}), "create_drawing")
    _, drawing = active_document()
    title = str(value(drawing, "GetTitle"))
    scale = 0.5 if max(box["size_mm"]) > 150 else 1.0
    try:
        circle = require(insert_model_view({"view": "left", "x_mm": 300, "y_mm": 180, "model_path": part,
                                            "scale": scale}), "insert_model_view")["data"]["view"]["name"]
        answer = insert_section_view({"parent_view": circle, "through_model_mm": centre, "direction": "vertical",
                                      "place_x_mm": 120, "place_y_mm": 180, "label": "A"})
        report["section"] = {"ok": answer["ok"], "message": answer["message"],
                             "face_hatches": answer.get("data", {}).get("view", {}).get("face_hatches"),
                             "parent_section_lines": answer.get("data", {}).get("parent_section_lines")}
        section = answer.get("data", {}).get("view", {}).get("name")
        views_before = len(require(list_drawing_views({}), "list_drawing_views")["data"]["views"])
        answer = insert_section_view({"parent_view": circle, "x1_mm": 410, "y1_mm": 10, "x2_mm": 415, "y2_mm": 10,
                                      "place_x_mm": 120, "place_y_mm": 60, "label": "B"})
        views_after = len(require(list_drawing_views({}), "list_drawing_views")["data"]["views"])
        report["section_beside"] = {"ok": answer["ok"], "removed": answer.get("data", {}).get("removed"),
                                    "line_removed": answer.get("data", {}).get("line_removed"),
                                    "parent_section_lines": answer.get("data", {}).get("parent_section_lines"),
                                    "views_before": views_before, "views_after": views_after}
        require(insert_model_annotations({"all_views": True}), "insert_model_annotations")
        listed = require(list_drawing_dimensions({}), "list_drawing_dimensions")["data"]
        duplicates = listed["duplicates"]
        report["dimensions_before"] = {"count": len(listed["dimensions"]), "duplicates": len(duplicates)}
        surplus = [{"view": v, "full_name": name} for name, views in duplicates.items()
                   for v in views if v != (section if section in views else views[0])]
        answer = hide_drawing_dimensions({"dimensions": surplus}) if surplus else {"ok": True, "data": {}}
        report["hide"] = {"ok": answer["ok"], "message": answer.get("message"),
                          "selected": answer.get("data", {}).get("selected")}
        listed = require(list_drawing_dimensions({}), "list_drawing_dimensions")["data"]
        report["dimensions_after"] = {"count": len(listed["dimensions"]), "duplicates": len(listed["duplicates"]),
                                      "outside_sheet": listed["outside_sheet"]}
        if section:
            answer = arrange_drawing_dimensions({"view_name": section})
            report["arrange"] = {k: answer.get(k) for k in ("ok", "message")} | {
                k: answer.get("data", {}).get(k) for k in ("arranged", "outside_before", "outside_after")}
        circle_view = next(v for v in require(list_drawing_views({}), "list_drawing_views")["data"]["views"]
                           if v["name"] == circle)
        x0, y0, x1, y1 = circle_view["outline_mm"]
        answer = insert_detail_view({"parent_view": circle, "center_x_mm": x1 - 5, "center_y_mm": (y0 + y1) / 2,
                                     "radius_mm": 8, "place_x_mm": 380, "place_y_mm": 60, "label": "Z",
                                     "scale_numerator": 1, "scale_denominator": 1})
        report["detail"] = {"ok": answer["ok"], "message": answer["message"]}
        answer = insert_section_view({"parent_view": circle, "through_model_mm": centre, "direction": "horizontal",
                                      "place_x_mm": 300, "place_y_mm": 40, "label": "C", "reverse": True})
        lines_with_c = answer.get("data", {}).get("parent_section_lines")
        again = insert_section_view({"parent_view": circle, "through_model_mm": centre, "direction": "vertical",
                                     "place_x_mm": 120, "place_y_mm": 60, "label": "A"})
        report["label_in_use"] = {"ok": again["ok"], "labels": again.get("data", {}).get("labels_in_use")}
        gone = delete_drawing_view({"name": answer.get("data", {}).get("view", {}).get("name", "")})
        lines_after = next(v for v in require(list_drawing_views({}), "list_drawing_views")["data"]["views"]
                           if v["name"] == circle)["section_lines"]
        report["delete_view"] = {"section_ok": answer["ok"], "lines_with_c": lines_with_c, "delete_ok": gone["ok"],
                                 "line_removed": gone.get("data", {}).get("line_removed"), "lines_after": lines_after}
        pdf = OUTPUT_ROOT / "live_drawing_clean" / f"section_{Path(part).stem}.pdf"
        report["pdf"] = str(pdf) if _save_as(drawing, str(pdf)) else None
        # a second sheet: its dimensions are listed with those of the first
        before = len(require(list_drawing_dimensions({}), "list_drawing_dimensions")["data"]["dimensions"])
        require(add_sheet({"name": "Blatt2", "paper_size": "A3"}), "add_sheet")
        other = require(insert_model_view({"view": "front", "x_mm": 200, "y_mm": 150, "model_path": part,
                                           "scale": scale}), "insert_model_view")["data"]["view"]["name"]
        added = insert_model_annotations({"all_views": False, "view_name": other})
        listed = require(list_drawing_dimensions({}), "list_drawing_dimensions")["data"]["dimensions"]
        report["second_sheet"] = {"added": added.get("data", {}).get("dimensions_added"), "before": before,
                                  "after": len(listed), "in_other_view": sum(1 for d in listed if d["view"] == other)}
        # acting on the first sheet while the second is active: its sheet is activated and the second again after
        target = next((d for d in listed if d["view"] == section), None)
        hidden = hide_drawing_dimensions({"dimensions": [{"view": section, "full_name": target["full_name"]}]})             if target else {"ok": False, "data": {}}
        arranged = arrange_drawing_dimensions({"view_name": section})
        current = str(value(value(drawing, "GetCurrentSheet"), "GetName"))
        report["inactive_sheet"] = {"hide_ok": hidden["ok"], "remaining": hidden.get("data", {}).get("remaining"),
                                    "arrange_ok": arranged["ok"], "current_sheet": current}
    finally:
        running_app().CloseDoc(title)
        report["part_dirty"] = part_dirty(part)
        if own_part and report["part_dirty"] is False:
            running_app().CloseDoc(Path(part).name)

    print(json.dumps(report, indent=2))
    checks = [
        report["cylinders_with_axis_point"] > 0,
        report["section"]["ok"] and (report["section"]["face_hatches"] or 0) > 0,
        report["section_beside"]["ok"] is False and report["section_beside"]["removed"]
        and report["section_beside"]["views_after"] == report["section_beside"]["views_before"]
        and report["section_beside"]["line_removed"] is True and report["section_beside"]["parent_section_lines"] == 1,
        report["dimensions_before"]["duplicates"] > 0,
        report["hide"]["ok"],
        report["dimensions_after"]["duplicates"] == 0,
        report.get("arrange", {}).get("ok") is True,
        report["detail"]["ok"],
        report["delete_view"]["section_ok"] and report["delete_view"]["delete_ok"]
        and report["delete_view"]["lines_after"] == report["delete_view"]["lines_with_c"] - 1,
        report["part_dirty"] is False,
        report["label_in_use"]["ok"] is False and "A" in (report["label_in_use"]["labels"] or []),
        report["inactive_sheet"]["hide_ok"] and report["inactive_sheet"]["arrange_ok"]
        and report["inactive_sheet"]["current_sheet"] == "Blatt2",
        report["second_sheet"]["in_other_view"] > 0
        and report["second_sheet"]["after"] == report["second_sheet"]["before"] + report["second_sheet"]["in_other_view"],
    ]
    print("checks", checks)
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
