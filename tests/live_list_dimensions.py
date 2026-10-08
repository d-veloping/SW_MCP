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

"""Live check: list_dimensions reports `driven` for exactly the reference (driven) dimensions (ClauSW #76).

A new part gets a sketch on the front plane with a rectangle away from the origin, fully defined with the ordinate
scheme in both directions; SOLIDWORKS 2016 creates the zero ordinates of such sets as driven reference dimensions
without a dialog (measured in the ClauSW sketch dimensioning spike).  The script then compares list_dimensions with
DrivenState read independently straight off the display dimensions, and checks:

(a) at least one dimension is reported `driven: true`;
(b) the dimensions reported `driven: true` are exactly those whose independent DrivenState is 1;
(c) every other dimension is reported `driven: false`;
(d) `driven_state` equals the independent DrivenState for every dimension.

Run this only on a workstation with SOLIDWORKS already running and nothing else driving it:

    ..\\.venv\\Scripts\\python.exe tests\\live_list_dimensions.py

The part is closed without saving at the end.  The output is one JSON object; exit code 0 when all four checks hold.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp.sw_core import active_document, flag_methods, iter_feature_objects, value
from solidworks_mcp.sw_file import close_document, create_new_document
from solidworks_mcp.sw_sketch import (close_sketch, create_sketch, draw_rectangle, fully_define_sketch,
                                      list_dimensions)


def independent_states() -> dict[str, int]:
    """DrivenState of every display dimension, read directly over COM, by full name."""
    _, doc = active_document()
    states: dict[str, int] = {}
    for feature in iter_feature_objects(doc):
        try:
            display = value(feature, "GetFirstDisplayDimension")
        except Exception:
            continue
        while display is not None:
            dimension = flag_methods(display, "GetDimension2").GetDimension2(0)
            states[str(dimension.FullName)] = int(dimension.DrivenState)
            display = flag_methods(feature, "GetNextDisplayDimension").GetNextDisplayDimension(display)
    return states


def main() -> int:
    report: dict[str, Any] = {"steps": {}}
    created = create_new_document({"kind": "part"})
    report["steps"]["create_new_document"] = created.get("message")
    title = ((created.get("data") or {}).get("document") or {}).get("title")
    if not created.get("ok"):
        print(json.dumps(report, indent=2))
        return 2
    try:
        for name, call, args in (
            ("create_sketch", create_sketch, {"plane": "front"}),
            ("draw_rectangle", draw_rectangle, {"x1_mm": 20, "y1_mm": 15, "x2_mm": 60, "y2_mm": 45}),
            ("fully_define_sketch", fully_define_sketch, {"horizontal_scheme": "ordinate", "vertical_scheme": "ordinate"}),
            ("close_sketch", close_sketch, {}),
        ):
            answer = call(args)
            report["steps"][name] = {"ok": bool(answer.get("ok")), "message": answer.get("message")}
        listed = (list_dimensions({}).get("data") or {}).get("dimensions") or []
        states = independent_states()
        report["dimensions"] = [{"full_name": d["full_name"], "driven": d["driven"], "driven_state": d["driven_state"],
                                 "independent": states.get(d["full_name"])} for d in listed]
        reported = {d["full_name"] for d in listed if d["driven"] is True}
        reference = {n for n, s in states.items() if s == 1}
        report["checks"] = {
            "a_some_driven": bool(reported),
            "b_driven_exactly_reference": reported == reference,
            "c_others_false": all(d["driven"] is False for d in listed if d["full_name"] not in reference),
            "d_raw_matches": all(d["driven_state"] == states.get(d["full_name"]) for d in listed),
            "same_set": {d["full_name"] for d in listed} == set(states),
        }
    finally:
        if title:
            report["steps"]["close_document"] = close_document({"title": title}).get("ok")
    print(json.dumps(report, indent=2))
    return 0 if all(report.get("checks", {}).values()) and report.get("checks") else 1


if __name__ == "__main__":
    raise SystemExit(main())
