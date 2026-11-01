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

"""Live check: set_sheet_format, set_dimension_style, move_dimension_text, list_view_arcs and reference_points_mm.

Without --part a new part gets a plate 60 x 40 x 10 mm (front plane, extruded 10) with a through hole of radius 6 at
(20, 20), dimensioned by its radius in the sketch, and a fillet of radius 8 on the edge at (60, 40); it is saved to a temporary folder under the server's
output root, closed and opened again. With --part a copy of that part is opened instead. The sheet format is always a
copy in the temporary folder (--template, by default the installed "a3 - iso.slddrt"), never the original. A drawing
gets the front view at --scale and the model's dimensions. The script checks:

(a) the own part has the volume of plate minus hole minus fillet (geometry, not the tools' answers; skipped with
    --part);
(b) set_sheet_format puts the copy on the sheet (A3, first angle) and a .txt path is refused;
(c) list_view_arcs lists true-shape arcs in the front view with no unreadable edge, every tessellated point on its
    circle; for the own part the hole (radius 6 x scale) and the fillet arc, whose middle lies at 45 degrees between
    its ends, i.e. on the material side, not on the complementary arc;
(d) a radius dimension (type2 5) exists, its reference_points_mm are readable;
(e) set_dimension_style turns shortened_radius and solid_leader off on it, and after (f) shortened_radius alone back
    on, and solid_leader stays off;
(f) move_dimension_text puts its text 8 mm beyond the middle of its arc (without a drawn arc: 8 mm beyond its last
    reference point, away from the first), and list_drawing_dimensions reads the text there too;
(g) a call naming another document is refused with active_document, and no call that should succeed failed.

Measured, not checked: whether the sheet format change moves views or dimension texts, whether SOLIDWORKS changes
the template file (size and modification time of the copy), and where the radius text sits after each style change.

Run this only on a workstation with SOLIDWORKS already running and nothing else driving it:

    ..\\.venv\\Scripts\\python.exe tests\\live_drawing_layout.py [--part P.sldprt] [--template F.slddrt] [--scale S]
                                                         [--view front]

Drawing and part are closed without saving and the temporary folder is removed. The output is one JSON object; exit
code 0 when all checks hold.
"""

from __future__ import annotations

import argparse
import glob
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
from solidworks_mcp.sw_drawing import (create_drawing, insert_model_annotations, insert_model_view,
                                       list_drawing_dimensions, list_drawing_views, list_view_arcs,
                                       move_dimension_text, set_dimension_style, set_sheet_format)
from solidworks_mcp.sw_feature import boss_extrude, cut_extrude, fillet
from solidworks_mcp.sw_file import (close_document, create_new_document, list_open_documents, open_document,
                                    save_document)
from solidworks_mcp.sw_inspect import get_mass_properties, list_edges
from solidworks_mcp.sw_sketch import (add_dimension, close_sketch, create_sketch, draw_circle, draw_rectangle,
                                      list_sketch_segments)

VOLUME_MM3 = 60 * 40 * 10 - math.pi * 6 ** 2 * 10 - (8 ** 2 - math.pi * 8 ** 2 / 4) * 10
INSTALLED_FORMAT = "C:/ProgramData/SOLIDWORKS/SOLIDWORKS */lang/*/sheetformat/a3 - iso.slddrt"


def open_titles() -> list[str]:
    """Titles of the open documents; a listing that fails raises, so nobody else's document counts as new."""
    answer = list_open_documents({})
    if not answer.get("ok"):
        raise RuntimeError(f"list_open_documents failed: {answer.get('message')}")
    return [d["title"] for d in (answer.get("data") or {}).get("documents") or []]


def data(answer: dict[str, Any]) -> dict[str, Any]:
    return answer.get("data") or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--part")
    parser.add_argument("--template")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--view", default="front", help="Standard view to place (front, top, right, ...).")
    options = parser.parse_args()
    template = options.template or next(iter(sorted(glob.glob(INSTALLED_FORMAT))), None)
    if not template or not Path(template).is_file():
        print(json.dumps({"error": f"no sheet format found ({template or INSTALLED_FORMAT}); pass --template"}))
        return 1

    report: dict[str, Any] = {"steps": {}, "checks": {}, "measured": {}}
    before = set(open_titles())
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="live_layout_", dir=OUTPUT_ROOT))
    failed: list[str] = []

    def step(name: str, call: Any, args: dict[str, Any], expect: bool = True) -> dict[str, Any]:
        answer = call(args)
        report["steps"][name] = {"ok": bool(answer.get("ok")), "message": answer.get("message")}
        if bool(answer.get("ok")) != expect:
            failed.append(name)
        return answer

    try:
        if options.part:
            part = folder / Path(options.part).name
            shutil.copy2(options.part, part)
        else:
            part = folder / "layout.sldprt"
            for name, call, args in (
                ("create_new_document", create_new_document, {"kind": "part"}),
                ("create_sketch", create_sketch, {"plane": "front"}),
                ("draw_rectangle", draw_rectangle, {"x1_mm": 0, "y1_mm": 0, "x2_mm": 60, "y2_mm": 40}),
                ("close_sketch", close_sketch, {}),
                ("boss_extrude", boss_extrude, {"depth_mm": 10}),
                ("create_sketch_hole", create_sketch, {"plane": "front"}),
                ("draw_circle", draw_circle, {"x_mm": 20, "y_mm": 20, "radius_mm": 6}),
                ("add_dimension", add_dimension, {"kind": "radius", "value_mm": 6, "place_x_mm": 35, "place_y_mm": 30}),
                ("close_sketch_hole", close_sketch, {}),
                ("cut_extrude", cut_extrude, {"depth_mm": 10}),
            ):
                if failed:
                    return 1
                if call is add_dimension:
                    segments = data(list_sketch_segments({})).get("segments") or []
                    args = {**args, "selection": {"sketch_segments": [s["index"] for s in segments]}}
                step(name, call, args)
            corner = [e["index"] for e in data(list_edges({"curve_type": "line"})).get("edges") or []
                      if abs(e.get("length_mm", 0) - 10) < 1e-6 and e.get("point_mm")
                      and abs(e["point_mm"][0] - 60) < 1e-6 and abs(e["point_mm"][1] - 40) < 1e-6]
            report["corner_edges"] = corner
            if failed or len(corner) != 1:
                failed.append("corner edge")
                return 1
            step("fillet", fillet, {"radius_mm": 8, "selection": {"edges": corner}})
            step("save_document", save_document, {"path": str(part), "overwrite": True})
            volume = data(get_mass_properties({})).get("volume_mm3")
            report["volume_mm3"] = volume
            report["checks"]["a_volume"] = isinstance(volume, (int, float)) and abs(volume - VOLUME_MM3) < 1.0
            if failed or not report["checks"]["a_volume"]:
                return 1
            step("close_part", close_document, {"title": "layout"})
        format_copy = folder / Path(template).name
        shutil.copy2(template, format_copy)
        stat_before = format_copy.stat()
        for name, call, args in (
            ("open_document", open_document, {"path": str(part)}),
            ("create_drawing", create_drawing, {"paper_size": "A3", "first_angle": True}),
        ):
            step(name, call, args)
            if failed:
                return 1
        drawing = data(step("insert_model_view", insert_model_view,
                            {"view": options.view, "x_mm": 160, "y_mm": 150, "model_path": str(part),
                             "scale": options.scale, "display_mode": "hidden_lines_removed"})).get("view") or {}
        view = drawing.get("name")
        if failed or not view:
            return 1
        step("insert_model_annotations", insert_model_annotations, {"all_views": True})
        views_before = {v["name"]: v.get("position_mm") for v in data(list_drawing_views({})).get("views") or []}
        texts_before = {d.get("full_name"): d.get("text_mm") for d in data(list_drawing_dimensions({})).get("dimensions")
                        or []}

        sheet = step("set_sheet_format", set_sheet_format, {"template_path": str(format_copy), "paper_size": "A3",
                                                            "first_angle": True})
        refused = step("set_sheet_format .txt", set_sheet_format, {"template_path": str(folder / "x.txt")},
                       expect=False)
        report["sheet"] = data(sheet)
        report["checks"]["b_sheet_format"] = (bool(sheet.get("ok")) and data(sheet).get("sheet_size_mm") == [420.0, 297.0]
                                             and Path(data(sheet).get("template", "")).name.lower()
                                             == format_copy.name.lower() and not refused.get("ok"))
        views_after = {v["name"]: v.get("position_mm") for v in data(list_drawing_views({})).get("views") or []}
        listed = data(list_drawing_dimensions({})).get("dimensions") or []
        texts_after = {d.get("full_name"): d.get("text_mm") for d in listed}
        stat_after = format_copy.stat()
        report["measured"].update(
            views_moved={n: [views_before.get(n), p] for n, p in views_after.items() if views_before.get(n) != p},
            texts_moved={n: [texts_before.get(n), p] for n, p in texts_after.items() if texts_before.get(n) != p},
            template_changed=[stat_before.st_size, stat_before.st_mtime] != [stat_after.st_size, stat_after.st_mtime],
        )

        arcs = data(step("list_view_arcs", list_view_arcs, {"view_name": view}))
        report["arcs"] = {k: arcs.get(k) for k in ("edges", "oblique", "unreadable")}
        report["arcs"]["found"] = [{k: a[k] for k in ("centre_mm", "radius_mm", "mid_mm", "full_circle")}
                                   for a in arcs.get("arcs") or []]
        on_circle = all(abs(math.dist(p, a["centre_mm"]) - a["radius_mm"]) < 0.05 for a in arcs.get("arcs") or []
                        for p in a["points_mm"] + [a["mid_mm"]])
        own = True
        if not options.part and options.view == "front":
            s = options.scale
            transform = next((v.get("model_to_sheet") for v in data(list_drawing_views({})).get("views") or []
                              if v["name"] == view), None) or {}
            t = transform.get("translation_mm") or [0, 0]
            hole = [a for a in arcs.get("arcs") or [] if a["full_circle"] and abs(a["radius_mm"] - 6 * s) < 0.01]
            bend = [a for a in arcs.get("arcs") or [] if not a["full_circle"] and abs(a["radius_mm"] - 8 * s) < 0.01]
            middle = [t[0] + s * (52 + 8 / math.sqrt(2)), t[1] + s * (32 + 8 / math.sqrt(2))]
            report["fillet_middle_expected"] = middle
            # front and back face may both list their circle and arc: they lie on top of each other
            own = bool(hole) and bool(bend) and all(math.dist(b["mid_mm"], middle) < 0.05 for b in bend)
        # a given part may draw no circle in true shape in this view: then the list is empty, and that is checked
        report["checks"]["c_arcs"] = (bool(arcs.get("arcs") or options.part) and arcs.get("unreadable") == 0
                                      and on_circle and own)

        radius = next((d for d in listed if d.get("view") == view and d.get("type2") == 5 and d.get("readable")), None)
        report["radius"] = radius
        report["checks"]["d_reference_points"] = bool(radius) and bool(radius.get("reference_points_mm"))
        if not radius:
            return 1
        name = radius["full_name"]
        off = step("set_dimension_style off", set_dimension_style,
                   {"view_name": view, "dimensions": [name], "shortened_radius": False, "solid_leader": False})
        listed_text = next((d.get("text_mm") for d in data(list_drawing_dimensions({"view_name": view})).get(
            "dimensions") or [] if d.get("full_name") == name), None)
        report["measured"]["text_after_style_off"] = [radius.get("text_mm"), listed_text]

        points = radius.get("reference_points_mm") or []
        circle = min(arcs.get("arcs") or [], default=None,
                     key=lambda a: min((math.dist(a["centre_mm"], p) for p in points), default=math.inf))
        if circle and points and min(math.dist(circle["centre_mm"], p) for p in points) < 0.5:
            c, m = circle["centre_mm"], circle["mid_mm"]
            report["text_rule"] = "arc middle + 8 mm"
        elif len(points) >= 2 and math.dist(points[0], points[-1]) > 0.5:
            c, m = points[0], points[-1]
            report["text_rule"] = "last reference point + 8 mm, away from the first"
        else:
            c, m = [0.0, 0.0], [0.0, 0.0]
        length = math.dist(c, m)
        target = ([round(m[0] + (m[0] - c[0]) / length * 8, 2), round(m[1] + (m[1] - c[1]) / length * 8, 2)]
                  if length else [300.0, 200.0])
        moved = step("move_dimension_text", move_dimension_text,
                     {"view_name": view, "moves": [{"full_name": name, "x_mm": target[0], "y_mm": target[1]}]})
        again = next((d.get("text_mm") for d in data(list_drawing_dimensions({"view_name": view})).get("dimensions")
                      or [] if d.get("full_name") == name), None)
        report["move"] = {"target": target, "answer": (data(moved).get("moves") or [{}])[0], "listed": again}
        report["checks"]["f_move"] = bool(moved.get("ok")) and bool(again) and math.dist(again, target) <= 0.5

        on = step("set_dimension_style shortened on", set_dimension_style,
                  {"view_name": view, "dimensions": [name], "shortened_radius": True})
        entry = (data(on).get("dimensions") or [{}])[0]
        report["style"] = {"off": (data(off).get("dimensions") or [{}])[0], "on": entry}
        report["checks"]["e_style"] = (bool(off.get("ok")) and bool(on.get("ok")) and entry.get("shortened_radius") is True
                                       and entry.get("solid_leader") is False)
        report["measured"]["text_after_shortened_on"] = [again, next(
            (d.get("text_mm") for d in data(list_drawing_dimensions({"view_name": view})).get("dimensions") or []
             if d.get("full_name") == name), None)]

        other = step("set_sheet_format other document", set_sheet_format,
                     {"template_path": str(format_copy), "document": "no such drawing"}, expect=False)
        report["checks"]["g_refused_and_no_failure"] = "active_document" in data(other) and not failed
    finally:
        try:
            for title in sorted(set(open_titles()) - before, key=lambda t: "Draw" not in t and " - " not in t):
                # the drawing first, then the part it references
                report["steps"][f"close {title}"] = close_document({"title": title}).get("ok")
            report["left_open"] = sorted(set(open_titles()) - before)
        except RuntimeError as exc:   # which documents are new is not known: close none
            report["left_open"] = [f"unknown: {exc}"]
        report["failed"] = failed
        shutil.rmtree(folder, ignore_errors=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["checks"] and all(report["checks"].values()) and not report["left_open"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
