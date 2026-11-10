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

"""Offline tests for hole_wizard (#40).

The handler runs for real against a fake part: a feature tree, a solid volume and body count, a FeatureManager whose
HoleWizard5 records its 27 arguments and builds a fake hole with IWizardHoleFeatureData2 fields, and a small Toolbox
database written to a temporary folder with the tables and columns of the SOLIDWORKS 2016 SP3 swbrowser.sldedb. Only
the position sketch (sketch on the face, points in sketch coordinates) is replaced as a whole; selecting its points
runs the real code. Every failure path has to end with ok false and the tree exactly as before. Whether SOLIDWORKS
builds the holes is the live check's job (tests/live_hole_wizard.py).
"""

from __future__ import annotations

import math
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_core, sw_feature  # noqa: E402
from solidworks_mcp.sw_core import ToolFailure  # noqa: E402

PLATE_MM3 = 60_000.0
TREE = ["Origin", "Sketch1", "Boss-Extrude1"]


def make_toolbox(folder: Path) -> None:
    """lang/english/swbrowser.sldedb with the ISO hole tables the tool reads, values as on 2016 SP3."""
    path = folder / "lang" / "english"
    path.mkdir(parents=True)
    db = sqlite3.connect(path / "swbrowser.sldedb")
    db.executescript("""
        CREATE TABLE ISO_Types (CategoryID TEXT, Name TEXT, ID TEXT, HasHoles INTEGER);
        INSERT INTO ISO_Types VALUES ('ISO_TYPE_BS', 'Hexagon Socket Head Screws', 'ISO_BS_SHS', 1),
            ('ISO_TYPE_NUTS', 'Hex Nuts', 'ISO_NUTS_HEX', 0), ('ISO_TYPE_HOLES', 'Straight Holes', 'ISO_HW_HOLES', 1),
            ('ISO_TYPE_HOLES', 'Tapped Holes', 'ISO_HW_TAPS', 1);
        CREATE TABLE ISO_Type_Holes (ID TEXT, enabled INTEGER, HasHoles INTEGER, ValidHoleTypes INTEGER, Name TEXT,
            Title TEXT, DataTable TEXT, SWconst_Enum_Value INTEGER);
        INSERT INTO ISO_Type_Holes VALUES
            ('ISO_HW_HOLES', 1, 1, 4, 'Screw Clearances', 'Screw Clearances', '+DATA_HW_ScrewClearances', 144),
            ('ISO_HW_TAPS', 1, 1, 8, 'Tapped hole', 'Tapped hole', '+DATA_HW_TappedHole', 147),
            ('ISO_HW_HOLES', 0, 1, 4, 'Dowel Holes', 'Dowel Holes', '+DATA_HW_DowelHole', 710);
        CREATE TABLE ISO_TYPE_BS (ID TEXT, enabled INTEGER, Name TEXT, Title TEXT, DataTable TEXT,
            SWConst_Enum_Value INTEGER, ValidHoleTypes INTEGER, HasHoles INTEGER);
        INSERT INTO ISO_TYPE_BS VALUES
            ('ISO_BS_SHS', 1, 'Cap (4762)', 'Hex Socket Head ISO 4762', '+DATA_BS_SHCSCREW4762', 139, 1, 1),
            ('ISO_BS_SHS', 1, 'CTSK (10642)', 'Hex Socket CTSK Head ISO 10642', '+DATA_BS_SCHSCREW10642', 140, 2, 1);
        CREATE TABLE ISO_DATA_HW_ScrewClearances (SIZE TEXT, CLOSE_FIT TEXT, NORMAL_FIT TEXT, enabled INTEGER);
        INSERT INTO ISO_DATA_HW_ScrewClearances VALUES ('M5', '5.3', '5.5', 1), ('M6', '6.4', '6.6', 1),
            ('M7', '7.4', '7.6', 0);
        CREATE TABLE ISO_DATA_HW_TappedHole (SIZE TEXT, Pitch TEXT, DIAMETER TEXT, enabled INTEGER);
        INSERT INTO ISO_DATA_HW_TappedHole VALUES ('M6', '1', '6', 1);
        CREATE TABLE ISO_DATA_BS_SHCSCREW4762 (SIZE TEXT, CBORE_DIA TEXT, CBORE_DEPTH TEXT, enabled INTEGER);
        INSERT INTO ISO_DATA_BS_SHCSCREW4762 VALUES ('M6', '11', '6.4', 1);
        CREATE TABLE ISO_DATA_BS_SCHSCREW10642 (SIZE TEXT, HEAD_DIA TEXT, HEAD_ANG TEXT);
        INSERT INTO ISO_DATA_BS_SCHSCREW10642 VALUES ('M6', '13.44', '90');
    """)
    db.commit()
    db.close()


class FakeApp:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def GetUserPreferenceStringValue(self, preference: int) -> str:
        assert preference == sw_feature.SW_HOLE_WIZARD_TOOLBOX_FOLDER
        return str(self.folder)


class FakePoint:
    def __init__(self, x_mm: float, y_mm: float, z_mm: float, selectable: bool = True, model: Any = None) -> None:
        self.X, self.Y, self.Z = x_mm / 1000, y_mm / 1000, z_mm / 1000
        self.selectable = selectable
        self.model = model

    def _FlagAsMethod(self, name: str) -> None:
        pass

    def Select4(self, append: bool, data: Any) -> bool:
        if self.selectable and self.model is not None:
            self.model.selected.append(self)
        return self.selectable


class FakeDefinition:
    """IWizardHoleFeatureData2: fields in metres and radians, -1 or 0 where they do not apply."""

    def __init__(self, fields: dict[str, Any], points_mm: list[list[float]]) -> None:
        self.__dict__.update(fields)
        self._points = [FakePoint(*p) for p in points_mm]

    def GetSketchPoints(self) -> list[FakePoint]:
        return self._points


class FakeFeature:
    def __init__(self, name: str, definition: FakeDefinition | None = None, subfeatures: list[str] | None = None):
        self.Name = name
        self._definition = definition
        self._subs = [FakeFeature(s) for s in (subfeatures or [])]
        for current, following in zip(self._subs, self._subs[1:] + [None]):
            current._next = following

    def GetDefinition(self) -> FakeDefinition | None:
        return self._definition

    def GetTypeName2(self) -> str:
        return "HoleWzd" if self._definition is not None else "ProfileFeature"

    def GetFirstSubFeature(self) -> Any:
        return self._subs[0] if self._subs else None

    def GetNextSubFeature(self) -> Any:
        return getattr(self, "_next", None)


# What SOLIDWORKS 2016 SP3 reads back for the spike's holes (#253) and the countersink probe of #40.
READBACK = {
    "clearance": ("M6 Clearance Hole1", 855.2986, {
        "Type": 25, "FastenerType2": 144, "FastenerType": "Screw Clearances", "EndCondition": 1,
        "ThruHoleDiameter": 0.0066, "ThruHoleDepth": 0.025, "HoleDiameter": 0.0, "HoleDepth": 0.0,
        "DrillAngle": 0.0, "HoleFit": 1}, []),
    "tapped": ("M6 Tapped Hole1", 363.2607, {
        "Type": 46, "FastenerType2": 147, "FastenerType": "Tapped hole", "EndCondition": 0,
        "TapDrillDiameter": 0.005, "TapDrillDepth": 0.018, "ThruTapDrillDiameter": 0.0, "ThruTapDrillDepth": 0.0,
        "ThreadDiameter": 0.006, "ThreadDepth": 0.015, "CosmeticThreadType": 1, "ThreadEndCondition": 0,
        "DrillAngle": math.radians(118)}, ["Hole Thread1"]),
    "counterbore": ("CBORE for M6 Hex Socket Head Cap Screw1", 1244.5545, {
        "Type": 14, "FastenerType2": 139, "FastenerType": "Hex Socket Head ISO 4762", "EndCondition": 1,
        "ThruHoleDiameter": 0.0066, "ThruHoleDepth": 0.025, "HoleDiameter": 0.0, "HoleDepth": 0.0,
        "CounterBoreDiameter": 0.011, "CounterBoreDepth": 0.0064, "HeadClearance": 0.0, "HoleFit": 1}, []),
    "countersink": ("CSK for M6 Hex Socket Countersunk Cap Screw1", 1018.4478, {
        "Type": 44, "FastenerType2": 140, "FastenerType": "Hex Socket CTSK Head ISO 10642", "EndCondition": 1,
        "ThruHoleDiameter": 0.0066, "ThruHoleDepth": 0.025, "HoleDiameter": 0.0, "HoleDepth": 0.0,
        "CounterSinkDiameter": 0.01344, "CounterSinkAngle": math.pi / 2, "HeadClearance": 0.0,
        "HeadClearanceType": 0, "HoleFit": 1}, []),
}

REQUESTS = {
    "clearance": {"kind": "clearance", "standard": "iso", "fastener": "Screw Clearances", "size": "M6",
                  "end_condition": "through_all"},
    "tapped": {"kind": "tapped", "standard": "iso", "fastener": "Tapped hole", "size": "M6", "end_condition": "blind",
               "depth_mm": 18, "thread_depth_mm": 15},
    "counterbore": {"kind": "counterbore", "standard": "iso", "fastener": "Hex Socket Head ISO 4762", "size": "M6",
                    "end_condition": "through_all"},
    "countersink": {"kind": "countersink", "standard": "iso", "fastener": "hex socket ctsk head iso 10642",
                    "size": "M6", "end_condition": "through_all"},
}


class FakeModel:
    """The part: feature names, solid volume, body count, selection, and what HoleWizard5 does to them."""

    def __init__(self) -> None:
        self.names = list(TREE)
        self.objects: dict[str, FakeFeature] = {}
        self.volume: float | None = PLATE_MM3
        self.bodies = 1
        self.selected: list[Any] = []
        self.calls: list[tuple] = []
        self.kind = "clearance"
        self.hole: dict[str, Any] = {}       # overrides for the next hole: returns, definition, removed, bodies, fields
        self.point_selectable = True

    def add(self, feature: FakeFeature) -> None:
        self.names.append(feature.Name)
        self.objects[feature.Name] = feature

    def place_points(self, doc: Any, face: Any, points_mm: list[list[float]]) -> tuple[str, list[FakePoint]]:
        self.add(FakeFeature("Sketch2"))
        self.points_mm = [list(p) for p in points_mm]
        return "Sketch2", [FakePoint(*p, selectable=self.point_selectable, model=self) for p in points_mm]

    def HoleWizard5(self, *args: Any) -> Any:
        self.calls.append(args)
        if self.hole.get("returns", True) is None:
            return None
        name, removed, fields, subs = READBACK[self.kind]
        fields = {"Standard": "ISO", "Standard2": 8, "FastenerSize": "M6", **fields, **self.hole.get("fields", {})}
        definition = None if self.hole.get("definition", True) is None else FakeDefinition(
            fields, self.hole.get("points_mm", self.points_mm))
        feature = FakeFeature(name, definition, subs)
        self.add(feature)
        count = len(self.points_mm)
        self.volume -= self.hole.get("removed", removed * count)
        self.bodies = self.hole.get("bodies", self.bodies)
        return feature

    # stand-ins for the sw_core tree helpers
    def feature_names(self, doc: Any) -> list[str]:
        return list(self.names)

    def features_added(self, doc: Any, before: list[str]) -> list[FakeFeature]:
        return [self.objects.get(n, FakeFeature(n)) for n in self.names if n not in set(before)]

    def delete_features(self, doc: Any, features: list[FakeFeature]) -> bool:
        gone = {f.Name for f in features}
        self.names = [n for n in self.names if n not in gone]
        return True


class FakeSurface:
    def __init__(self, identity: int = 4001) -> None:
        self.Identity = identity
        self.PlaneParams = (0.0, 1.0, 0.0, 0.0, 0.025, 0.0)


class FakeFace:
    def __init__(self, identity: int = 4001) -> None:
        self._surface = FakeSurface(identity)

    def GetSurface(self) -> FakeSurface:
        return self._surface


class HoleWizardHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.toolbox = Path(self._tmp.name) / "SOLIDWORKS Data"
        make_toolbox(self.toolbox)
        self.model = FakeModel()
        self.faces = [(FakeFace(), None), (FakeFace(4002), None)]
        self.whats_wrong: list[dict[str, Any]] = []
        self.require_part = unittest.mock.Mock(return_value=(FakeApp(self.toolbox), object()))
        model = self.model
        patches = [
            unittest.mock.patch.object(sw_feature, "require_part", self.require_part),
            unittest.mock.patch.object(sw_feature, "feature_manager", side_effect=lambda doc: model),
            unittest.mock.patch.object(sw_feature, "exit_active_sketch", return_value=None),
            unittest.mock.patch.object(sw_feature, "clear_selection", side_effect=lambda doc: model.selected.clear()),
            unittest.mock.patch.object(sw_feature, "rebuild", return_value=True),
            unittest.mock.patch.object(sw_feature, "whats_wrong", side_effect=lambda doc: list(self.whats_wrong)),
            unittest.mock.patch.object(sw_feature, "iter_face_objects", side_effect=lambda doc: self.faces),
            unittest.mock.patch.object(sw_feature, "_place_points", side_effect=model.place_points),
            unittest.mock.patch.object(sw_feature, "selected_objects", side_effect=lambda doc: list(model.selected)),
            unittest.mock.patch.object(sw_feature, "nothing", return_value=None),
            unittest.mock.patch.object(sw_feature, "_solid_state",
                                       side_effect=lambda doc: (model.volume, model.bodies)),
            unittest.mock.patch.object(sw_feature, "feature_names", side_effect=model.feature_names),
            unittest.mock.patch.object(sw_feature, "features_added", side_effect=model.features_added),
            unittest.mock.patch.object(sw_feature, "delete_features", side_effect=model.delete_features),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def drill(self, kind: str, points: list[list[float]] | None = None, **extra: Any) -> dict[str, Any]:
        self.model.kind = kind
        args = {**REQUESTS[kind], "face_index": 0,
                "points": [{"x_mm": x, "y_mm": y, "z_mm": z} for x, y, z in (points or [[0.0, 25.0, 0.0]])]}
        args.update(extra)
        return sw_feature.hole_wizard(args)

    def assert_clean_failure(self, answer: dict[str, Any], phrase: str) -> None:
        self.assertFalse(answer["ok"], answer)
        self.assertIn(phrase, answer["message"])
        self.assertEqual(self.model.names, TREE, "the tree must be exactly as before")
        self.assertEqual(answer["data"].get("left_behind", []), [])


class SuccessTests(HoleWizardHarness):
    """Acceptance 1: one success per kind with the full readback."""

    def test_clearance(self) -> None:
        answer = self.drill("clearance")
        self.assertTrue(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual(data["feature"], "M6 Clearance Hole1")
        self.assertEqual(data["position_sketch"], "Sketch2")
        self.assertEqual(data["type"], {"value": 25, "name": "swHoleThru"})
        self.assertEqual(data["fastener_index"], 144)
        self.assertEqual(data["hole_data"], {"ThruHoleDiameter_mm": 6.6, "ThruHoleDepth_mm": 25.0,
                                             "HoleDiameter_mm": 0.0, "HoleDepth_mm": 0.0, "DrillAngle_deg": 0.0,
                                             "HoleFit": 1})
        self.assertEqual(data["points_mm"], [[0.0, 25.0, 0.0]])
        self.assertEqual(data["volume_before_mm3"], PLATE_MM3)
        self.assertAlmostEqual(data["volume_after_mm3"], PLATE_MM3 - 855.2986)
        self.assertAlmostEqual(data["removed_mm3"], 855.2986)
        self.assertEqual(data["bodies"], 1)
        self.assertEqual(data["fit"], "normal")
        self.assertEqual(self.model.names, TREE + ["Sketch2", "M6 Clearance Hole1"])
        [call] = self.model.calls
        self.assertEqual(len(call), 27)
        self.assertEqual(call[:8], (2, 8, 144, "M6", 1, 0.0, 0.0, 0.0))
        self.assertEqual(call[8:20], (1.0, -1.0) + (0.0,) * 10)   # fit normal, drill angle left to the standard
        self.assertEqual(call[20:], ("", False, True, True, True, True, False))

    def test_tapped(self) -> None:
        answer = self.drill("tapped")
        self.assertTrue(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual(data["type"], {"value": 46, "name": "swTapBlindCosmeticThread"})
        self.assertEqual(data["hole_data"], {
            "TapDrillDiameter_mm": 5.0, "TapDrillDepth_mm": 18.0, "ThruTapDrillDiameter_mm": 0.0,
            "ThruTapDrillDepth_mm": 0.0, "ThreadDiameter_mm": 6.0, "ThreadDepth_mm": 15.0, "CosmeticThreadType": 1,
            "ThreadEndCondition": 0, "DrillAngle_deg": 118.0})
        self.assertEqual(data["subfeatures"], [{"name": "Hole Thread1", "type": "ProfileFeature"}])
        self.assertTrue(data["cosmetic"])
        self.assertNotIn("fit", data)
        [call] = self.model.calls
        self.assertEqual(call[:8], (4, 8, 147, "M6", 0, -1.0, 0.018, -1.0))
        self.assertEqual(call[8:20], (0.015, -1.0, -1.0, -1.0, -1.0, -1.0, 1.0, 0.0, -1.0, -1.0, -1.0, -1.0))

    def test_counterbore(self) -> None:
        answer = self.drill("counterbore")
        self.assertTrue(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual(data["type"]["value"], 14)
        self.assertEqual(data["hole_data"]["CounterBoreDiameter_mm"], 11.0)
        self.assertEqual(data["hole_data"]["CounterBoreDepth_mm"], 6.4)
        self.assertEqual(data["hole_data"]["HeadClearance_mm"], 0.0)
        self.assertEqual(data["hole_data"]["ThruHoleDiameter_mm"], 6.6)
        [call] = self.model.calls
        self.assertEqual(call[:8], (0, 8, 139, "M6", 1, -1.0, -1.0, -1.0))
        self.assertEqual(call[8:20], (-1.0, -1.0, 0.0, 1.0) + (-1.0,) * 8)

    def test_countersink(self) -> None:
        answer = self.drill("countersink")
        self.assertTrue(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual(data["type"], {"value": 44, "name": "swCounterSinkThruWithoutHeadClearance"})
        self.assertEqual(data["fastener"], "hex socket ctsk head iso 10642")
        self.assertEqual(data["fastener_index"], 140)
        self.assertEqual(data["hole_data"]["CounterSinkDiameter_mm"], 13.44)
        self.assertEqual(data["hole_data"]["CounterSinkAngle_deg"], 90.0)
        self.assertEqual(data["hole_data"]["HeadClearanceType"], 0)
        [call] = self.model.calls
        self.assertEqual(call[:8], (1, 8, 140, "M6", 1, -1.0, -1.0, -1.0))
        self.assertEqual(call[8:20], (-1.0, -1.0, 0.0, 1.0) + (-1.0,) * 8)

    def test_two_points_in_one_call(self) -> None:
        self.model.hole = {"fields": {"EndCondition": 0, "HoleFit": 2, "HoleDiameter": 0.0066, "HoleDepth": 0.01}}
        with unittest.mock.patch.object(sw_feature, "rename_feature", return_value="Lochbild") as rename:
            answer = self.drill("clearance", [[-15.0, 25.0, 0.0], [15.0, 25.0, 5.0]], end_condition="blind",
                                depth_mm=10, fit="loose", name="Lochbild")
        self.assertTrue(answer["ok"], answer)
        rename.assert_called_once()
        self.assertEqual(answer["data"]["feature"], "Lochbild")
        self.assertEqual(answer["data"]["points_mm"], [[-15.0, 25.0, 0.0], [15.0, 25.0, 5.0]])
        self.assertAlmostEqual(answer["data"]["removed_mm3"], 2 * 855.2986)
        self.assertEqual(answer["data"]["depth_mm"], 10.0)
        [call] = self.model.calls
        self.assertEqual(call[4:8], (0, 0.0, 0.01, 0.0))
        self.assertEqual(call[8], 2.0)

    def test_face_by_point(self) -> None:
        with unittest.mock.patch.object(sw_feature, "select_at_point", return_value=True) as pick, \
                unittest.mock.patch.object(sw_feature, "selected_objects", return_value=[FakeFace()]):
            answer = sw_feature.hole_wizard({**REQUESTS["clearance"], "face_point_mm": [10, 25, 10],
                                             "points": [{"x_mm": 0, "y_mm": 25, "z_mm": 0}]})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(pick.call_args.args[1:3], ("FACE", [0.01, 0.025, 0.01]))


class FailureTests(HoleWizardHarness):
    """Acceptance 1: every failure path ends with ok false and the tree as before."""

    def test_holewizard5_returns_none(self) -> None:
        self.model.hole = {"returns": None}
        answer = self.drill("clearance")
        self.assert_clean_failure(answer, "HoleWizard5 returned no feature")
        self.assertEqual(answer["data"]["deleted"], ["Sketch2"])

    def test_definition_unreadable(self) -> None:
        self.model.hole = {"definition": None}
        answer = self.drill("counterbore")
        self.assert_clean_failure(answer, "no definition")
        self.assertEqual(answer["data"]["deleted"], ["Sketch2", "CBORE for M6 Hex Socket Head Cap Screw1"])

    def test_one_field_unreadable(self) -> None:
        self.model.hole = {"fields": {"CounterBoreDepth": None}}
        self.assert_clean_failure(self.drill("counterbore"), "CounterBoreDepth")

    def test_volume_unchanged(self) -> None:
        self.model.hole = {"removed": 0.0}
        answer = self.drill("tapped")
        self.assert_clean_failure(answer, "removed no material")
        self.assertEqual(answer["data"]["volume_after_mm3"], PLATE_MM3)

    def test_volume_unmeasurable_after(self) -> None:
        self.model.hole = {"removed": 0.0}
        original = self.model.HoleWizard5

        def hole_then_unmeasurable(*args: Any) -> Any:
            feature = original(*args)
            self.model.volume = None
            return feature
        self.model.HoleWizard5 = hole_then_unmeasurable
        self.assert_clean_failure(self.drill("clearance"), "cannot be measured after")

    def test_body_count_changed(self) -> None:
        self.model.hole = {"bodies": 2}
        self.assert_clean_failure(self.drill("countersink"), "changed the body count from 1 to 2")

    def test_rebuild_errors(self) -> None:
        original = self.model.HoleWizard5

        def hole_with_error(*args: Any) -> Any:
            self.whats_wrong.append({"feature": "M6 Clearance Hole1", "code": 1, "is_warning": False})
            return original(*args)
        self.model.HoleWizard5 = hole_with_error
        answer = self.drill("clearance")
        self.assert_clean_failure(answer, "rebuild errors")
        self.assertEqual(answer["data"]["problems"][0]["feature"], "M6 Clearance Hole1")

    def test_problems_that_were_there_before_do_not_count(self) -> None:
        self.whats_wrong.append({"feature": "Boss-Extrude1", "code": 2, "is_warning": True})
        answer = self.drill("clearance")
        self.assertTrue(answer["ok"], answer)

    def test_face_index_out_of_range(self) -> None:
        answer = self.drill("clearance", face_index=7)
        self.assertFalse(answer["ok"])
        self.assertIn("out of range", answer["message"])
        self.assertEqual(self.model.names, TREE)
        self.assertEqual(self.model.calls, [])

    def test_face_not_selectable_by_point(self) -> None:
        with unittest.mock.patch.object(sw_feature, "select_at_point", return_value=False):
            answer = sw_feature.hole_wizard({**REQUESTS["clearance"], "face_point_mm": [99, 99, 99],
                                             "points": [{"x_mm": 0, "y_mm": 25, "z_mm": 0}]})
        self.assertFalse(answer["ok"])
        self.assertIn("No face could be selected", answer["message"])
        self.assertEqual(self.model.names, TREE)

    def test_face_not_planar(self) -> None:
        answer = self.drill("clearance", face_index=1)
        self.assertFalse(answer["ok"])
        self.assertIn("not planar", answer["message"])
        self.assertEqual(self.model.names, TREE)

    def test_point_off_the_plane(self) -> None:
        answer = self.drill("clearance", [[0.0, 25.0, 0.0], [0.0, 20.0, 0.0]])
        self.assertFalse(answer["ok"])
        self.assertIn("off the face's plane: [[0.0, 20.0, 0.0]]", answer["message"])
        self.assertEqual(self.model.names, TREE)
        self.assertEqual(self.model.calls, [])

    def test_point_not_selectable(self) -> None:
        self.model.point_selectable = False
        answer = self.drill("clearance")
        self.assert_clean_failure(answer, "Only 0 of 1 hole centres could be selected")
        self.assertEqual(answer["data"]["deleted"], ["Sketch2"])
        self.assertEqual(self.model.calls, [])

    def test_position_sketch_incomplete(self) -> None:
        def short(doc: Any, face: Any, points: list[list[float]]) -> Any:
            self.model.add(FakeFeature("Sketch2"))
            raise ToolFailure("The position sketch holds 1 points instead of 2.", position_sketch="Sketch2")
        with unittest.mock.patch.object(sw_feature, "_place_points", side_effect=short):
            answer = self.drill("clearance", [[0.0, 25.0, 0.0], [5.0, 25.0, 0.0]])
        self.assert_clean_failure(answer, "holds 1 points instead of 2")

    def test_com_error_mid_way(self) -> None:
        def broken(*args: Any) -> Any:
            raise RuntimeError("RPC server unavailable")
        self.model.HoleWizard5 = broken
        self.assert_clean_failure(self.drill("clearance"), "SOLIDWORKS COM error: RPC server unavailable")

    def test_unknown_size(self) -> None:
        answer = self.drill("clearance", size="M6x1.0")
        self.assertFalse(answer["ok"])
        self.assertIn("Unknown size 'M6x1.0' for 'Screw Clearances'", answer["message"])
        self.assertEqual(answer["data"]["sizes"], ["M5", "M6"])     # M7 is disabled in the table
        self.assertEqual(self.model.names, TREE)
        self.assertEqual(self.model.calls, [])

    def test_unknown_fastener_and_wrong_kind(self) -> None:
        answer = self.drill("clearance", fastener="Hex Socket Head ISO 4762")
        self.assertFalse(answer["ok"])
        self.assertIn("offers no clearance hole", answer["message"])
        self.assertEqual(answer["data"]["fasteners_for_kind"], ["Screw Clearances"])
        answer = self.drill("clearance", fastener="Dowel Holes")    # disabled in the table
        self.assertIn("No fastener table 'Dowel Holes'", answer["message"])
        self.assertEqual(self.model.calls, [])

    def test_toolbox_folder_is_the_language_folder(self) -> None:
        # what 2016 SP3 reports for swHoleWizardToolBoxFolder: <Toolbox>\lang\english, not the Toolbox root
        self.require_part.return_value = (FakeApp(self.toolbox / "lang" / "english"), object())
        self.assertTrue(self.drill("clearance")["ok"])

    def test_no_toolbox_database(self) -> None:
        self.require_part.return_value = (FakeApp(Path(self._tmp.name) / "elsewhere"), object())
        answer = self.drill("clearance")
        self.assertFalse(answer["ok"])
        self.assertIn("No Toolbox database", answer["message"])


class ReadbackTests(HoleWizardHarness):
    """SOLIDWORKS built a hole, but not the one asked for: refused and deleted."""

    CASES = [
        ({"FastenerSize": "M5"}, "size 'M5' instead of 'M6'"),
        ({"FastenerType2": 145}, "fastener type 145 instead of 144"),
        ({"EndCondition": 0}, "end condition 0 instead of through_all"),
        ({"HoleFit": 0}, "fit 0 instead of normal"),
    ]

    def test_mismatches(self) -> None:
        for fields, phrase in self.CASES:
            with self.subTest(fields=fields):
                self.model.names, self.model.volume = list(TREE), PLATE_MM3
                self.model.hole = {"fields": fields}
                self.assert_clean_failure(self.drill("clearance"), phrase)

    def test_thread_mismatches(self) -> None:
        for fields, phrase in (({"ThreadDepth": 0.012}, "thread depth 12.0 mm instead of 15"),
                               ({"CosmeticThreadType": 0}, "cosmetic thread type 0 instead of 1")):
            with self.subTest(fields=fields):
                self.model.names, self.model.volume = list(TREE), PLATE_MM3
                self.model.hole = {"fields": fields}
                self.assert_clean_failure(self.drill("tapped"), phrase)

    def test_points_mismatch(self) -> None:
        self.model.hole = {"points_mm": [[0.0, 25.0, 0.0], [9.0, 25.0, 0.0]]}
        self.assert_clean_failure(self.drill("clearance"), "holes nobody asked for at [[9.0, 25.0, 0.0]] mm")
        self.model.names, self.model.volume = list(TREE), PLATE_MM3
        self.model.hole = {"points_mm": [[1.0, 25.0, 0.0]]}
        self.assert_clean_failure(self.drill("clearance"), "no hole at [0.0, 25.0, 0.0] mm")


class ArgumentTests(HoleWizardHarness):
    """Invalid requests are refused before SOLIDWORKS is touched."""

    INVALID = [
        ({"kind": "slot"}, "kind must be one of"),
        ({"standard": "ansi"}, "standard must be one of"),
        ({"size": ""}, "fastener and size are required"),
        ({"end_condition": "up_to_next"}, "end_condition must be blind or through_all"),
        ({"end_condition": "blind"}, "needs depth_mm"),
        ({"depth_mm": 5}, "depth_mm only applies to end_condition blind"),
        ({"depth_mm": 0, "end_condition": "blind"}, "depth_mm must be a finite number greater than 0"),
        ({"fit": "snug"}, "fit must be one of"),
        ({"thread_depth_mm": 5}, "only apply to a tapped hole"),
        ({"cosmetic": False}, "only apply to a tapped hole"),
        ({"face_index": None}, "exactly one of face_index"),
        ({"face_point_mm": [0, 25, 0]}, "exactly one of face_index"),
        ({"face_index": -1}, "face_index must be a non-negative integer"),
        ({"face_index": True}, "face_index must be a non-negative integer"),
        ({"points": []}, "points needs at least one hole centre"),
        ({"points": [{"x_mm": 0, "y_mm": 25}]}, "points[0] must be three finite numbers"),
        ({"points": [{"x_mm": 0, "y_mm": float("nan"), "z_mm": 0}]}, "points[0] must be three finite numbers"),
    ]
    INVALID_TAPPED = [
        ({"fit": "normal"}, "fit does not apply to a tapped hole"),
        ({"thread_depth_mm": None}, "needs thread_depth_mm"),
        ({"thread_depth_mm": 20}, "cannot be deeper than the drilled depth_mm"),
        ({"cosmetic": "yes"}, "cosmetic must be true or false"),
    ]

    def check(self, base: dict[str, Any], cases: list[tuple[dict[str, Any], str]]) -> None:
        for extra, phrase in cases:
            with self.subTest(extra=extra):
                args = {**base, "face_index": 0, "points": [{"x_mm": 0, "y_mm": 25, "z_mm": 0}], **extra}
                args = {k: v for k, v in args.items() if v is not None}
                answer = sw_feature.hole_wizard(args)
                self.assertFalse(answer["ok"], answer)
                self.assertIn(phrase, answer["message"])
                self.require_part.assert_not_called()

    def test_invalid(self) -> None:
        self.check(REQUESTS["clearance"], self.INVALID)

    def test_invalid_tapped(self) -> None:
        self.check(REQUESTS["tapped"], self.INVALID_TAPPED)

    def test_schema(self) -> None:
        [tool] = [t for t in sw_core.TOOLS if t.name == "hole_wizard"]
        schema = tool.inputSchema
        self.assertEqual(schema["required"], ["kind", "standard", "fastener", "size", "end_condition", "points"])
        self.assertEqual(schema["properties"]["kind"]["enum"], ["clearance", "counterbore", "countersink", "tapped"])
        self.assertIn("position sketch", tool.description)
        self.assertIn("HoleWizard5", sw_core._FEATURE_MANAGER_METHODS)


class SelectPointsTests(unittest.TestCase):
    def test_partial_selection_is_refused(self) -> None:
        model = FakeModel()
        points = [FakePoint(0, 25, 0, model=model), FakePoint(5, 25, 0, selectable=False, model=model)]
        with unittest.mock.patch.object(sw_feature, "clear_selection"), \
                unittest.mock.patch.object(sw_feature, "nothing", return_value=None), \
                unittest.mock.patch.object(sw_feature, "selected_objects", side_effect=lambda doc: model.selected):
            with self.assertRaisesRegex(ToolFailure, "Only 1 of 2 hole centres"):
                sw_feature._select_points(object(), points)


if __name__ == "__main__":
    unittest.main()
