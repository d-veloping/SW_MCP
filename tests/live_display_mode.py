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

"""Live check: display styles of drawing views read back as requested (issue #4).

Run this only on a workstation with SOLIDWORKS already running and nothing
else driving it, with a saved part (a revolved part with a bore shows the
hidden lines best):

    ..\\.venv\\Scripts\\python.exe tests\\live_display_mode.py C:\\path\\part.sldprt

Steps: an A3 drawing gets the standard views with hidden_lines_visible and is
saved as <output root>/live_display_mode/hidden_lines_visible.pdf to look at;
every display style is set on the first view through set_drawing_view; one
model view and one projected view are placed with a style.  The drawing is
closed without saving.
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

from solidworks_mcp.sw_core import OUTPUT_ROOT, active_document, running_app, value
from solidworks_mcp.sw_drawing import (
    DISPLAY_MODES,
    create_drawing,
    insert_model_view,
    insert_projected_view,
    insert_standard_views,
    set_drawing_view,
)
from solidworks_mcp.sw_file import _save_as, open_document


def require(payload: dict[str, Any], context: str) -> dict[str, Any]:
    if not payload.get("ok"):
        raise SystemExit(f"{context} failed: {payload.get('message')}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("part", help="a saved .sldprt")
    args = parser.parse_args()
    part = str(Path(args.part).resolve())
    report: dict[str, Any] = {}

    require(open_document({"path": part}), "open_document")
    require(create_drawing({"paper_size": "A3", "first_angle": True}), "create_drawing")
    _, drawing = active_document()
    title = str(value(drawing, "GetTitle"))
    try:
        answer = insert_standard_views({"model_path": part, "first_angle": True, "display_mode": "hidden_lines_visible"})
        report["standard_views"] = {"ok": answer["ok"], "message": answer["message"],
                                    "display": answer.get("data", {}).get("display")}
        pdf = OUTPUT_ROOT / "live_display_mode" / "hidden_lines_visible.pdf"
        report["pdf"] = str(pdf) if _save_as(drawing, str(pdf)) else None
        first = report["standard_views"]["display"][0]["view"]
        report["set_drawing_view"] = {}
        for name in [*DISPLAY_MODES, "hidden_lines_grey"]:
            answer = set_drawing_view({"name": first, "display_mode": name})
            report["set_drawing_view"][name] = {"ok": answer["ok"], "read_back": answer["data"]["view"].get("display_mode")}
        answer = insert_model_view({"view": "isometric", "x_mm": 330, "y_mm": 220, "model_path": part,
                                    "display_mode": "shaded_with_edges"})
        report["model_view"] = {"ok": answer["ok"], "read_back": answer.get("data", {}).get("view", {}).get("display_mode")}
        parent = answer.get("data", {}).get("view", {}).get("name")
        answer = insert_projected_view({"parent_view": parent, "direction": "down", "offset_mm": 90, "display_mode": "wireframe"})
        report["projected_view"] = {"ok": answer["ok"], "message": answer["message"],
                                    "read_back": answer.get("data", {}).get("view", {}).get("display_mode")}
    finally:
        running_app().CloseDoc(title)

    print(json.dumps(report, indent=2))
    checks = [
        report["standard_views"]["ok"] and all(o["actual"] == "hidden_lines_visible" for o in report["standard_views"]["display"]),
        all(r["ok"] for r in report["set_drawing_view"].values()),
        all(r["read_back"] == name for name, r in report["set_drawing_view"].items() if name in DISPLAY_MODES),
        report["model_view"]["ok"] and report["model_view"]["read_back"] == "shaded_with_edges",
        report["projected_view"]["ok"] and report["projected_view"]["read_back"] == "wireframe",
    ]
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
