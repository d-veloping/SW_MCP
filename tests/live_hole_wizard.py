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

"""Live check: hole_wizard (#40) on a plate 60 x 40 x 25 mm (top plane, top face at y = 25).

Every hole is one hole_wizard call; after each the removed volume is compared with the closed-form volume of the
standard geometry (Toolbox values of SOLIDWORKS 2016 SP3) and the body count with 1:

(a) clearance ISO 'Screw Clearances' M6 normal, through all, at (-18, 25, 0), face by index: cylinder 6.6 x 25 =
    855.2986 mm3 (spike #253).
(b) tapped ISO 'Tapped hole' M6, blind 18, thread 15, cosmetic, at (0, 25, 0): tap drill 5 x 18 to the shoulder plus
    the 118-degree tip = 363.2607 mm3, sub-feature CosmeticThread (spike #253).
(c) counterbore ISO 'Hex Socket Head ISO 4762' M6, through all, at (18, 25, 0): 11 x 6.4 + 6.6 x 18.6 = 1244.5545 mm3
    (spike #253; the table has 6.4, not 6.5).
(d) countersink ISO 'Hex Socket CTSK Head ISO 10642' M6, through all, at (0, 25, -12): 90-degree cone from 13.44 (head
    diameter in the table) to 6.6 over 3.42, then 6.6 x 21.58 = 1018.4478 mm3.
(e) hole pattern: clearance M6 normal, blind 10, at (-18, 25, 12) and (18, 25, 12) in one call: twice 6.6 x 10 plus the
    118-degree tip = 729.4634 mm3, one feature, two points read back.
(f) a request whose point lies on the face's plane but off the body is refused (ok false), the tree and the volume
    stay as they were.
(g) after saving, closing and reopening: the same features, total volume, body count, no rebuild errors, and every
    hole's IWizardHoleFeatureData2 fields and points as the tool reported them.

Each answer is ok true with the feature name from HoleWizard5, the position sketch in the tree in front of it, and the
hole data of its kind. The run stops at the first check that fails and reports nothing after it as done.

Run this only on a workstation with SOLIDWORKS already running and nothing else driving it:

    ..\\.venv\\Scripts\\python.exe tests\\live_hole_wizard.py

The output is one JSON object, also when a step raises; exit code 0 when every check holds.
"""

from __future__ import annotations

import json
import math
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import solidworks_mcp  # noqa: E402
import solidworks_mcp.server  # noqa: E402,F401  (registers every tool)
from solidworks_mcp import sw_feature  # noqa: E402
from solidworks_mcp.sw_core import HANDLERS, OUTPUT_ROOT, active_document, find_feature, running_app, value  # noqa: E402

PLATE = (60.0, 40.0, 25.0)
TOLERANCE_MM3 = 0.01


def cylinder(d: float, h: float) -> float:
    return math.pi * (d / 2) ** 2 * h


def tip(d: float, angle_deg: float = 118.0) -> float:
    r = d / 2
    return math.pi * r ** 2 * (r / math.tan(math.radians(angle_deg / 2))) / 3


def frustum(d_big: float, d_small: float, angle_deg: float) -> tuple[float, float]:
    """Volume and height of a countersink cone from d_big down to d_small."""
    height = (d_big - d_small) / 2 / math.tan(math.radians(angle_deg / 2))
    R, r = d_big / 2, d_small / 2
    return math.pi * height / 3 * (R * R + R * r + r * r), height


CSK_VOLUME, CSK_HEIGHT = frustum(13.44, 6.6, 90.0)
CASES: list[dict[str, Any]] = [
    {"label": "a_clearance", "removed": cylinder(6.6, 25.0),
     "args": {"kind": "clearance", "standard": "iso", "fastener": "Screw Clearances", "size": "M6",
              "end_condition": "through_all", "points": [[-18.0, 25.0, 0.0]]},
     "data": {"ThruHoleDiameter_mm": 6.6, "ThruHoleDepth_mm": 25.0, "HoleFit": 1}, "type": 25},
    {"label": "b_tapped", "removed": cylinder(5.0, 18.0) + tip(5.0),
     "args": {"kind": "tapped", "standard": "iso", "fastener": "Tapped hole", "size": "M6", "end_condition": "blind",
              "depth_mm": 18, "thread_depth_mm": 15, "cosmetic": True, "points": [[0.0, 25.0, 0.0]]},
     "data": {"TapDrillDiameter_mm": 5.0, "TapDrillDepth_mm": 18.0, "ThreadDiameter_mm": 6.0, "ThreadDepth_mm": 15.0,
              "CosmeticThreadType": 1, "ThreadEndCondition": 0, "DrillAngle_deg": 118.0}, "type": 46,
     "subfeature": "CosmeticThread"},
    {"label": "c_counterbore", "removed": cylinder(11.0, 6.4) + cylinder(6.6, 25.0 - 6.4),
     "args": {"kind": "counterbore", "standard": "iso", "fastener": "Hex Socket Head ISO 4762", "size": "M6",
              "end_condition": "through_all", "points": [[18.0, 25.0, 0.0]]},
     "data": {"ThruHoleDiameter_mm": 6.6, "CounterBoreDiameter_mm": 11.0, "CounterBoreDepth_mm": 6.4,
              "HeadClearance_mm": 0.0, "HoleFit": 1}, "type": 14},
    {"label": "d_countersink", "removed": CSK_VOLUME + cylinder(6.6, 25.0 - CSK_HEIGHT),
     "args": {"kind": "countersink", "standard": "iso", "fastener": "Hex Socket CTSK Head ISO 10642", "size": "M6",
              "end_condition": "through_all", "points": [[0.0, 25.0, -12.0]]},
     "data": {"ThruHoleDiameter_mm": 6.6, "CounterSinkDiameter_mm": 13.44, "CounterSinkAngle_deg": 90.0,
              "HoleFit": 1}, "type": 44},
    {"label": "e_pattern", "removed": 2 * (cylinder(6.6, 10.0) + tip(6.6)),
     "args": {"kind": "clearance", "standard": "iso", "fastener": "Screw Clearances", "size": "M6",
              "end_condition": "blind", "depth_mm": 10, "points": [[-18.0, 25.0, 12.0], [18.0, 25.0, 12.0]],
              "name": "Lochbild M6"},
     "data": {"HoleDiameter_mm": 6.6, "HoleDepth_mm": 10.0, "DrillAngle_deg": 118.0, "HoleFit": 1}, "type": 22},
]
EXPECTED_SPIKE = {"a_clearance": 855.2986, "b_tapped": 363.2607, "c_counterbore": 1244.5545}


class Stop(RuntimeError):
    """A check failed; the run ends here."""


class Run:
    def __init__(self) -> None:
        self.report: dict[str, Any] = {"module": solidworks_mcp.__file__, "steps": [], "checks": {}, "holes": {}}

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = HANDLERS[name](args or {})
        self.report["steps"].append({"tool": name, "ok": answer.get("ok"), "message": answer.get("message")})
        return answer

    def require(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = self.call(name, args)
        if not answer.get("ok"):
            raise Stop(f"{name} failed: {answer.get('message')}")
        return answer.get("data") or {}

    def check(self, label: str, passed: bool, detail: Any = None) -> None:
        self.report["checks"][label] = passed
        if not passed:
            raise Stop(f"check {label} failed: {detail}")

    def state(self) -> tuple[float, int, list[str]]:
        bodies = self.require("list_bodies")
        names = [f["name"] for f in self.require("list_features")["features"]]
        return float(bodies["volume_sum_mm3"]), len(bodies["bodies"]), names

    def open_titles(self) -> list[str]:
        return [d["title"] for d in self.require("list_open_documents").get("documents") or []]


def top_face(run: Run) -> int:
    faces = run.require("list_faces", {"normal": [0, 1, 0]})["faces"]
    top = [f for f in faces if abs(f["point_mm"][1] - PLATE[2]) < 1e-3]
    run.check("top_face_unique", len(top) == 1, faces)
    return int(top[0]["index"])


def drill(run: Run, case: dict[str, Any], face: dict[str, Any]) -> dict[str, Any]:
    volume_before, bodies_before, names_before = run.state()
    args = {**case["args"], **face,
            "points": [{"x_mm": x, "y_mm": y, "z_mm": z} for x, y, z in case["args"]["points"]]}
    started = time.monotonic()
    answer = run.call("hole_wizard", args)
    seconds = round(time.monotonic() - started, 2)
    data = answer.get("data") or {}
    volume_after, bodies_after, names_after = run.state()
    removed = round(volume_before - volume_after, 4)
    label = case["label"]
    run.report["holes"][label] = {"ok": answer.get("ok"), "message": answer.get("message"), "seconds": seconds,
                                  "feature": data.get("feature"), "position_sketch": data.get("position_sketch"),
                                  "type": data.get("type"), "hole_data": data.get("hole_data"),
                                  "points_mm": data.get("points_mm"), "removed_mm3": removed,
                                  "expected_mm3": round(case["removed"], 4), "tool_removed_mm3": data.get("removed_mm3"),
                                  "bodies": bodies_after, "subfeatures": data.get("subfeatures")}
    run.check(f"{label}_ok", answer.get("ok") is True, answer.get("message"))
    run.check(f"{label}_volume", abs(removed - case["removed"]) <= TOLERANCE_MM3,
              f"removed {removed} mm3, expected {case['removed']:.4f}")
    if label in EXPECTED_SPIKE:
        run.check(f"{label}_spike_volume", abs(removed - EXPECTED_SPIKE[label]) <= TOLERANCE_MM3, removed)
    run.check(f"{label}_tool_volume", abs(float(data["removed_mm3"]) - removed) <= TOLERANCE_MM3, data["removed_mm3"])
    run.check(f"{label}_bodies", bodies_before == bodies_after == data.get("bodies") == 1, bodies_after)
    feature, sketch = data.get("feature"), data.get("position_sketch")
    new = [n for n in names_after if n not in names_before]
    wanted_name = case["args"].get("name") or feature
    run.check(f"{label}_tree", new == [sketch, feature] and feature == wanted_name, new)
    hole_data = data.get("hole_data") or {}
    run.check(f"{label}_data", all(abs(hole_data.get(k, math.inf) - v) <= 1e-6 for k, v in case["data"].items()),
              hole_data)
    if case["type"] is not None:
        run.check(f"{label}_type", (data.get("type") or {}).get("value") == case["type"], data.get("type"))
    points = sorted(data.get("points_mm") or [])
    run.check(f"{label}_points", len(points) == len(case["args"]["points"]) and all(
        all(abs(a - b) <= 1e-3 for a, b in zip(p, q)) for p, q in zip(points, sorted(case["args"]["points"]))), points)
    if case.get("subfeature"):
        run.check(f"{label}_subfeature", any(s.get("type") == case["subfeature"] for s in data.get("subfeatures") or []),
                  data.get("subfeatures"))
    return data


def readback(features: dict[str, str]) -> dict[str, Any]:
    """Every hole's data read straight from its feature, as the tool reads it."""
    _, doc = active_document()
    out = {}
    for name, kind in features.items():
        feature = find_feature(doc, name)
        if feature is None:
            raise Stop(f"{name} is not in the reopened part")
        identity, data, centres = sw_feature._hole_readback(feature, kind)
        out[name] = {"type": identity["Type"], "data": data, "points_mm": sorted(centres)}
    return out


def run_cases(run: Run, folder: Path) -> None:
    length, width, thickness = PLATE
    created = run.require("create_new_document", {"kind": "part"})
    run.report["part"] = (created.get("document") or {}).get("title")
    run.require("create_sketch", {"plane": "top"})
    run.require("draw_rectangle", {"x1_mm": -length / 2, "y1_mm": -width / 2, "x2_mm": length / 2, "y2_mm": width / 2})
    run.require("close_sketch")
    run.require("boss_extrude", {"depth_mm": thickness, "end_condition": "blind"})
    volume, bodies, _ = run.state()
    run.check("plate", abs(volume - length * width * thickness) <= TOLERANCE_MM3 and bodies == 1, volume)

    answers: dict[str, dict[str, Any]] = {}
    for case in CASES:
        # the first hole by face index, the others by a point on the top face away from every hole
        face = {"face_index": top_face(run)} if case is CASES[0] else {"face_point_mm": [-27.0, thickness, -17.0]}
        answers[case["label"]] = drill(run, case, face)

    volume, bodies, names = run.state()
    refused = run.call("hole_wizard", {**CASES[0]["args"], "face_point_mm": [-27.0, thickness, -17.0],
                                       "points": [{"x_mm": 100.0, "y_mm": thickness, "z_mm": 100.0}]})
    after = run.state()
    run.report["holes"]["f_refused"] = {"ok": refused.get("ok"), "message": refused.get("message"),
                                        "deleted": (refused.get("data") or {}).get("deleted"),
                                        "left_behind": (refused.get("data") or {}).get("left_behind")}
    run.check("f_refused", refused.get("ok") is False and after == (volume, bodies, names), refused.get("message"))

    expected_total = length * width * thickness - sum(c["removed"] for c in CASES)
    run.check("total_volume", abs(volume - expected_total) <= TOLERANCE_MM3, f"{volume} vs {expected_total:.4f}")
    problems = run.require("check_errors", {"rebuild_first": True}).get("problems")
    run.check("check_errors_before_save", problems == [], problems)
    features = {answers[c["label"]]["feature"]: c["args"]["kind"] for c in CASES}
    first = {"volume": round(volume, 4), "bodies": bodies, "features": names, "holes": readback(features)}
    for c in CASES:
        reported = answers[c["label"]]
        hole = first["holes"][reported["feature"]]
        run.check(f"{c['label']}_readback_matches_answer", hole["data"] == reported["hole_data"]
                  and hole["points_mm"] == sorted(reported["points_mm"]), hole)

    path = folder / "hole_wizard.sldprt"
    run.require("save_document", {"path": str(path), "overwrite": True})
    run.check("saved", path.is_file(), str(path))
    _, doc = active_document()
    saved_title = str(value(doc, "GetTitle"))
    run.require("close_document", {"title": saved_title})
    run.check("closed", saved_title not in run.open_titles(), saved_title)
    run.require("open_document", {"path": str(path)})
    problems = run.require("check_errors", {"rebuild_first": True}).get("problems")
    run.check("check_errors_after_reopen", problems == [], problems)
    volume, bodies, names = run.state()
    second = {"volume": round(volume, 4), "bodies": bodies, "features": names, "holes": readback(features)}
    differences = {k: {"before": first[k], "after": second[k]} for k in first if first[k] != second[k]}
    run.report["reopen_differences"] = differences
    run.check("g_reopened_same", not differences, differences)


def main() -> int:
    run = Run()
    app = running_app()
    run.report["solidworks"] = str(value(app, "RevisionNumber"))
    run.report["date"] = time.strftime("%Y-%m-%d %H:%M:%S")
    run.report["python"] = platform.python_version()
    before = set(run.open_titles())
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="live_hole_wizard_", dir=OUTPUT_ROOT))
    try:
        run_cases(run, folder)
        run.report["result"] = "all checks passed"
    except Exception as exc:  # noqa: BLE001  a failed check or a COM error: report, clean up, exit 1
        run.report["result"] = f"STOPPED: {type(exc).__name__}: {exc}"
    finally:
        for title in sorted(set(run.open_titles()) - before):
            run.report["steps"].append({f"close {title}": HANDLERS["close_document"]({"title": title}).get("ok")})
        run.report["left_open"] = sorted(set(run.open_titles()) - before)
        shutil.rmtree(folder, ignore_errors=True)
        print(json.dumps(run.report, indent=1, ensure_ascii=False, default=str))
    passed = run.report["result"] == "all checks passed" and not run.report["left_open"]
    print("LIVE HOLE WIZARD OK" if passed else f"LIVE HOLE WIZARD FAILED: {run.report['result']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
