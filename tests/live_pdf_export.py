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

"""Live check: export_document writes a drawing as PDF (issue #3).

Run this only on a workstation with SOLIDWORKS already running and nothing
else driving it, with a saved part:

    ..\\.venv\\Scripts\\python.exe tests\\live_pdf_export.py C:\\path\\part.sldprt

Steps: a .pdf of the part is refused without a file; an A3 drawing with the
standard views exports to one page of 420 x 297 mm; the same export with
overwrite returns without a dialog; with a second sheet the page count is
recorded once with the second and once with the first sheet active.  The page
size is read from /MediaBox without a PDF library; if SOLIDWORKS hides it in a
compressed object stream the check reports "skipped".  The drawing is closed
without saving; the PDFs stay under <output root>/live_pdf_export.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp.sw_core import OUTPUT_ROOT, active_document, running_app, value
from solidworks_mcp.sw_drawing import activate_sheet, add_sheet, create_drawing, insert_model_view, insert_standard_views
from solidworks_mcp.sw_file import export_document, open_document

A3_PT = (420 / 25.4 * 72, 297 / 25.4 * 72)
FOLDER = "live_pdf_export"


def require(payload: dict[str, Any], context: str) -> dict[str, Any]:
    if not payload.get("ok"):
        raise SystemExit(f"{context} failed: {payload.get('message')}")
    return payload


def pdf_facts(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    pages = len(re.findall(rb"/Type\s*/Page(?![s\w])", data))
    boxes = re.findall(rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", data)
    sizes = [(round(float(x1) - float(x0), 2), round(float(y1) - float(y0), 2)) for x0, y0, x1, y1 in boxes]
    a3 = [abs(w - A3_PT[0]) < 2 and abs(h - A3_PT[1]) < 2 for w, h in sizes]
    return {
        "bytes": len(data),
        "pdf_header": data[:5] == b"%PDF-",
        "pages": pages or "skipped",
        "page_sizes_pt": sizes or "skipped",
        "a3": (all(a3) if a3 else "skipped"),
    }


def export(name: str, overwrite: bool = True) -> dict[str, Any]:
    started = time.perf_counter()
    answer = export_document({"path": f"{FOLDER}/{name}", "overwrite": overwrite})
    entry = {"ok": answer.get("ok"), "message": answer.get("message"), "seconds": round(time.perf_counter() - started, 2)}
    target = OUTPUT_ROOT / FOLDER / name
    entry["file"] = target.is_file()
    if entry["ok"] and entry["file"]:
        entry.update(pdf_facts(target))
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("part", help="a saved .sldprt")
    args = parser.parse_args()
    part = str(Path(args.part).resolve())
    report: dict[str, Any] = {}

    require(open_document({"path": part}), "open_document")
    refused = OUTPUT_ROOT / FOLDER / "part.pdf"
    refused.unlink(missing_ok=True)
    report["part_refused"] = export("part.pdf")
    require(create_drawing({"paper_size": "A3", "first_angle": True}), "create_drawing")
    _, drawing = active_document()
    title = str(value(drawing, "GetTitle"))
    try:
        require(insert_standard_views({"model_path": part, "first_angle": True}), "insert_standard_views")
        report["one_sheet"] = export("one_sheet.pdf")
        report["one_sheet_overwrite"] = export("one_sheet.pdf")
        sheet_one = str(value(value(drawing, "GetCurrentSheet"), "GetName"))
        require(add_sheet({"name": "Zwei", "paper_size": "A3"}), "add_sheet")
        require(insert_model_view({"view": "isometric", "x_mm": 210, "y_mm": 150, "model_path": part}), "insert_model_view")
        report["two_sheets_second_active"] = export("two_sheets_second_active.pdf")
        require(activate_sheet({"name": sheet_one}), "activate_sheet")
        report["two_sheets_first_active"] = export("two_sheets_first_active.pdf")
    finally:
        running_app().CloseDoc(title)

    print(json.dumps(report, indent=2))
    checks = [
        report["part_refused"]["ok"] is False and not report["part_refused"]["file"],
        report["one_sheet"]["ok"] and report["one_sheet"]["pdf_header"] and report["one_sheet"]["pages"] in (1, "skipped"),
        report["one_sheet"]["a3"] in (True, "skipped"),
        bool(report["one_sheet_overwrite"]["ok"]),
        bool(report["two_sheets_second_active"]["ok"]) and bool(report["two_sheets_first_active"]["ok"]),
    ]
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
