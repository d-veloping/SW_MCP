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

"""Offline tests for the parametric sketch tools: set_dimension judges a change by readback and sketch state (#14).

The fakes reproduce what was measured on SOLIDWORKS 2016 SP3 on 2026-10-09 and 2026-10-10: SetSystemValue3 stores
the value and SystemValue reads it back at once; after EditRebuild3 SystemValue reads the stored value again, the old
one when the solver rejected the change, and the sketch state changes at that rebuild; GetConstrainedStatus of a
closed sketch is readable through the owning feature; the open sketch's solver rejects a value on the spot and the
open sketch reads its new state at once.
"""

from __future__ import annotations

import math
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_core, sw_sketch  # noqa: E402

FULLY, OVER, NO_SOLUTION = 3, 4, 5   # swConstrainedStatus_e


class FakeSketch:
    """An ISketch whose GetConstrainedStatus answers `code`; a test changes `code` to model the solver."""

    def __init__(self, code: int) -> None:
        self.code = code

    def GetConstrainedStatus(self) -> int:  # noqa: N802 - COM member name
        return self.code


class FakeOwner:
    def __init__(self, name: str, kind: str, sketch: FakeSketch | None = None) -> None:
        self.Name = name
        self._kind = kind
        self._sketch = sketch

    def GetTypeName2(self) -> str:  # noqa: N802 - COM member name
        return self._kind

    def GetSpecificFeature2(self):  # noqa: N802 - COM member name
        return self._sketch


class FakeDimension:
    """IDimension: SetSystemValue3 stores the value unless `rejects`, which models the open sketch's solver."""

    def __init__(self, system_value, owner, rejects: bool = False) -> None:
        self.SystemValue = system_value
        self._owner = owner
        self._rejects = rejects
        self.set_calls: list[float] = []
        self.owner_calls = 0

    def _FlagAsMethod(self, *names: str) -> None:
        pass

    def GetFeatureOwner(self):  # noqa: N802 - COM member name
        self.owner_calls += 1
        if isinstance(self._owner, Exception):
            raise self._owner
        return self._owner

    def SetSystemValue3(self, target: float, scope: int, config) -> int:  # noqa: N802 - COM member name
        self.set_calls.append(target)
        if not self._rejects:
            self.SystemValue = target
        return 0


class FakeSketchManager:
    def __init__(self, active) -> None:
        self.ActiveSketch = active


class FakeDoc:
    """IModelDoc2 with Parameter and EditRebuild3; `on_rebuild` models what the solver does during the rebuild."""

    def __init__(self, dimension: FakeDimension, active_sketch=None, rebuilt: bool = True, on_rebuild=None) -> None:
        self._dimension = dimension
        self.SketchManager = FakeSketchManager(active_sketch)
        self._rebuilt = rebuilt
        self._on_rebuild = on_rebuild
        self.rebuilds = 0

    def Parameter(self, name: str):  # noqa: N802 - COM member name
        return self._dimension

    def EditRebuild3(self) -> bool:  # noqa: N802 - COM member name
        self.rebuilds += 1
        if self._on_rebuild:
            self._on_rebuild()
        return self._rebuilt


def during_rebuild(dimension: FakeDimension | None = None, value=None, sketch: FakeSketch | None = None,
                   code: int | None = None):
    """What the measured rebuild does: the stored value reverts and the sketch state changes, both only then."""
    def _apply() -> None:
        if dimension is not None:
            dimension.SystemValue = value
        if sketch is not None and code is not None:
            sketch.code = code
    return _apply


class SetDimensionTests(unittest.TestCase):
    NAME = "D1@P_Skizze@Part5.Part"

    def call(self, doc: FakeDoc, **args) -> dict:
        with unittest.mock.patch.object(sw_sketch, "active_document", return_value=(None, doc)), \
                unittest.mock.patch.object(sw_sketch, "empty_variant", return_value=None):
            return sw_sketch.set_dimension({"full_name": self.NAME, **args})

    def sketch_owner(self, code: int, name: str = "P_Skizze") -> tuple[FakeOwner, FakeSketch]:
        sketch = FakeSketch(code)
        return FakeOwner(name, sw_core.SKETCH_2D_TYPE, sketch), sketch

    def test_closed_sketch_value_applied_and_fully_defined(self) -> None:
        owner, _ = self.sketch_owner(FULLY)
        dimension = FakeDimension(0.080, owner)
        doc = FakeDoc(dimension)
        answer = self.call(doc, value_mm=90)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(dimension.set_calls, [0.090])
        self.assertEqual(doc.rebuilds, 1)
        data = answer["data"]
        self.assertEqual(data["owner"], "P_Skizze")
        self.assertAlmostEqual(data["previous_value_mm"], 80.0)
        self.assertAlmostEqual(data["actual_value_mm"], 90.0)
        self.assertEqual((data["sketch_status_before"], data["sketch_status"]), ("fully_defined", "fully_defined"))

    def test_measured_fault_value_reverts_and_sketch_is_no_solution_at_the_rebuild(self) -> None:
        """The #14 reproduction: SystemValue reads 6 at once; the rebuild reverts it to 5 and the sketch turns no_solution."""
        owner, sketch = self.sketch_owner(FULLY)
        dimension = FakeDimension(0.005, owner)
        doc = FakeDoc(dimension, on_rebuild=during_rebuild(dimension, 5.0000000000000435e-3, sketch, NO_SOLUTION))
        answer = self.call(doc, value_mm=6)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(doc.rebuilds, 1)
        self.assertIn("no_solution", answer["message"])
        self.assertIn("P_Skizze", answer["message"])
        self.assertIn("did not move", answer["message"])
        data = answer["data"]
        self.assertEqual(data["sketch_status"], "no_solution")
        self.assertEqual(data["sketch_status_before"], "fully_defined")
        self.assertAlmostEqual(data["actual_value_mm"], 5.0, places=6)
        self.assertAlmostEqual(data["previous_value_mm"], 5.0)
        self.assertEqual(data["value_mm"], 6.0)

    def test_unsolved_status_after_the_rebuild_fails_even_when_the_value_reads_back(self) -> None:
        owner, sketch = self.sketch_owner(FULLY)
        doc = FakeDoc(FakeDimension(0.005, owner), on_rebuild=during_rebuild(sketch=sketch, code=OVER))
        answer = self.call(doc, value_mm=6)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["data"]["sketch_status"], "over_defined")
        self.assertAlmostEqual(answer["data"]["actual_value_mm"], 6.0)

    def test_reverted_value_alone_fails_even_when_the_sketch_solves(self) -> None:
        """A dimension driven by an equation reads the equation's value after the rebuild."""
        owner, _ = self.sketch_owner(FULLY)
        dimension = FakeDimension(0.005, owner)
        doc = FakeDoc(dimension, on_rebuild=during_rebuild(dimension, 0.005))
        answer = self.call(doc, value_mm=6)
        self.assertFalse(answer["ok"], answer)
        self.assertIn("did not apply", answer["message"])
        self.assertIn("5.0 mm", answer["message"])
        self.assertEqual(answer["data"]["sketch_status"], "fully_defined")

    def test_unreadable_value_after_the_rebuild_is_not_a_success(self) -> None:
        dimension = FakeDimension(0.010, FakeOwner("Boss-Extrude1", "Extrusion"))
        doc = FakeDoc(dimension, on_rebuild=during_rebuild(dimension, None))
        answer = self.call(doc, value_mm=12)
        self.assertFalse(answer["ok"], answer)
        self.assertIn("could not be read back", answer["message"])
        self.assertIsNone(answer["data"]["actual_value_mm"])

    def test_sketch_already_unsolved_before_the_change(self) -> None:
        owner, _ = self.sketch_owner(NO_SOLUTION)
        doc = FakeDoc(FakeDimension(0.005, owner))
        answer = self.call(doc, value_mm=5)
        self.assertFalse(answer["ok"], answer)
        self.assertIn("already no_solution", answer["message"])
        self.assertIn("edit_sketch", answer["message"])
        self.assertEqual(answer["data"]["sketch_status_before"], "no_solution")

    def test_owner_sketch_open_is_not_rebuilt_and_reports_its_status(self) -> None:
        owner, sketch = self.sketch_owner(FULLY)
        dimension = FakeDimension(0.090, owner)
        doc = FakeDoc(dimension, active_sketch=sketch)
        answer = self.call(doc, value_mm=85)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(doc.rebuilds, 0)
        self.assertIn("open sketch", answer["message"])
        self.assertEqual(answer["data"]["sketch_status"], "fully_defined")
        self.assertAlmostEqual(answer["data"]["actual_value_mm"], 85.0)

    def test_owner_sketch_open_rejection_names_the_state_the_open_sketch_reads(self) -> None:
        """Measured 2026-10-10: in the open, fully defined sketch the solver rejects 6 at once and reads no_solution."""
        owner, sketch = self.sketch_owner(FULLY)
        dimension = FakeDimension(0.005, owner, rejects=True)

        def reject(target, scope, config):
            dimension.set_calls.append(target)
            sketch.code = NO_SOLUTION
            return 0
        dimension.SetSystemValue3 = reject
        doc = FakeDoc(dimension, active_sketch=sketch)
        answer = self.call(doc, value_mm=6)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(doc.rebuilds, 0)
        self.assertIn("did not accept 6.0 mm", answer["message"])
        self.assertIn("no_solution", answer["message"])
        self.assertIn("still reads 5.0 mm", answer["message"])
        data = answer["data"]
        self.assertEqual((data["sketch_status_before"], data["sketch_status"]), ("fully_defined", "no_solution"))
        self.assertAlmostEqual(data["previous_value_mm"], 5.0)
        self.assertAlmostEqual(data["actual_value_mm"], 5.0)

    def test_feature_dimension_rejection_does_not_guess_a_sketch(self) -> None:
        dimension = FakeDimension(0.010, FakeOwner("Boss-Extrude1", "Extrusion"), rejects=True)
        answer = self.call(FakeDoc(dimension), value_mm=12)
        self.assertFalse(answer["ok"], answer)
        self.assertIn("'Boss-Extrude1'", answer["message"])
        self.assertNotIn("sketch", answer["message"])
        self.assertNotIn("sketch_status", answer["data"])

    def test_owner_sketch_open_and_still_unsolved_says_so_without_edit_sketch(self) -> None:
        """Live (h) of #16: the value is stored in the open, unsolved sketch, which keeps reading no_solution."""
        owner, sketch = self.sketch_owner(NO_SOLUTION)
        doc = FakeDoc(FakeDimension(0.005, owner), active_sketch=sketch)
        answer = self.call(doc, value_mm=5)
        self.assertFalse(answer["ok"], answer)
        self.assertIn("open sketch", answer["message"])
        self.assertIn("close_sketch", answer["message"])
        self.assertNotIn("edit_sketch", answer["message"])
        self.assertNotIn("not applied", answer["message"])
        self.assertEqual(answer["data"]["sketch_status"], "no_solution")

    def test_another_sketch_open_changes_nothing(self) -> None:
        owner, _ = self.sketch_owner(FULLY)
        dimension = FakeDimension(0.080, owner)
        doc = FakeDoc(dimension, active_sketch=FakeSketch(FULLY))
        answer = self.call(doc, value_mm=90)
        self.assertFalse(answer["ok"], answer)
        self.assertIn("does not own", answer["message"])
        self.assertIn("Nothing was changed", answer["message"])
        self.assertEqual(dimension.set_calls, [])
        self.assertEqual(doc.rebuilds, 0)
        self.assertEqual(answer["data"]["owner"], "P_Skizze")

    def test_any_sketch_open_blocks_a_feature_dimension(self) -> None:
        dimension = FakeDimension(0.010, FakeOwner("Boss-Extrude1", "Extrusion"))
        doc = FakeDoc(dimension, active_sketch=FakeSketch(FULLY))
        answer = self.call(doc, value_mm=12)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(dimension.set_calls, [])

    def test_unreadable_owner_with_a_sketch_open_is_refused_and_says_why(self) -> None:
        for owner in (None, RuntimeError("COM")):
            with self.subTest(owner=owner):
                dimension = FakeDimension(0.010, owner)
                answer = self.call(FakeDoc(dimension, active_sketch=FakeSketch(FULLY)), value_mm=12)
                self.assertFalse(answer["ok"], answer)
                self.assertIn("could not be read", answer["message"])
                self.assertNotIn("does not own", answer["message"])
                self.assertEqual(dimension.set_calls, [])

    def test_feature_dimension_is_judged_by_the_readback_after_the_rebuild(self) -> None:
        dimension = FakeDimension(0.010, FakeOwner("Boss-Extrude1", "Extrusion"))
        doc = FakeDoc(dimension)
        answer = self.call(doc, value_mm=12)
        self.assertTrue(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual(data["owner"], "Boss-Extrude1")
        self.assertNotIn("sketch_status", data)
        self.assertNotIn("sketch_status_before", data)
        self.assertAlmostEqual(data["actual_value_mm"], 12.0)

        reverting = FakeDimension(0.010, FakeOwner("Boss-Extrude1", "Extrusion"))
        doc = FakeDoc(reverting, on_rebuild=during_rebuild(reverting, 0.010))
        answer = self.call(doc, value_mm=12)
        self.assertFalse(answer["ok"], answer)
        self.assertIn("did not apply", answer["message"])

    def test_angular_dimension_reports_degrees(self) -> None:
        dimension = FakeDimension(math.radians(30), FakeOwner("Boss-Extrude1", "Extrusion"))
        answer = self.call(FakeDoc(dimension), value_deg=45)
        self.assertTrue(answer["ok"], answer)
        self.assertAlmostEqual(answer["data"]["previous_value_deg"], 30.0)
        self.assertAlmostEqual(answer["data"]["actual_value_deg"], 45.0)
        self.assertNotIn("actual_value_mm", answer["data"])

    def test_unreadable_owner_counts_as_a_feature_owner(self) -> None:
        for owner in (None, RuntimeError("COM")):
            with self.subTest(owner=owner):
                dimension = FakeDimension(0.010, owner)
                answer = self.call(FakeDoc(dimension), value_mm=12)
                self.assertTrue(answer["ok"], answer)
                self.assertEqual(answer["data"]["owner"], "")
                self.assertNotIn("sketch_status", answer["data"])

    def test_rebuild_false_fails_as_before(self) -> None:
        owner, _ = self.sketch_owner(FULLY)
        doc = FakeDoc(FakeDimension(0.080, owner), rebuilt=False)
        answer = self.call(doc, value_mm=90)
        self.assertFalse(answer["ok"], answer)
        self.assertIn("rebuild reported a problem", answer["message"])
        self.assertIs(answer["data"]["rebuilt"], False)

    def test_diametric_only_path_is_unchanged(self) -> None:
        """No owner lookup, no SetSystemValue3, and an open sketch does not block a pure restyle."""
        for active in (None, FakeSketch(FULLY)):
            with self.subTest(sketch_open=active is not None):
                dimension = FakeDimension(0.040, FakeOwner("Skizze9", sw_core.SKETCH_2D_TYPE, FakeSketch(FULLY)))
                display = unittest.mock.Mock(Diametric=False)
                doc = FakeDoc(dimension, active_sketch=active)
                with unittest.mock.patch.object(sw_sketch, "_display_dimension", return_value=display):
                    answer = self.call(doc, diametric=True)
                self.assertTrue(answer["ok"], answer)
                self.assertIs(display.Diametric, True)
                self.assertEqual(doc.rebuilds, 0 if active is not None else 1)
                self.assertEqual(dimension.set_calls, [])
                self.assertEqual(dimension.owner_calls, 0)
                self.assertEqual(answer["data"], {"value_mm": 40.0, "diametric": True})

    def test_another_sketch_open_blocks_before_a_restyle_with_value(self) -> None:
        owner, _ = self.sketch_owner(FULLY)
        dimension = FakeDimension(0.040, owner)
        display = unittest.mock.Mock(Diametric=False)
        doc = FakeDoc(dimension, active_sketch=FakeSketch(FULLY))
        with unittest.mock.patch.object(sw_sketch, "_display_dimension", return_value=display):
            answer = self.call(doc, diametric=True, value_mm=50)
        self.assertFalse(answer["ok"], answer)
        self.assertIs(display.Diametric, False)
        self.assertEqual(dimension.set_calls, [])

    def test_nothing_to_set(self) -> None:
        answer = self.call(FakeDoc(FakeDimension(0.040, None)))
        self.assertFalse(answer["ok"])
        self.assertIn("Pass value_mm", answer["message"])


UNDER, INVALID = 2, 6   # swConstrainedStatus_e


class FakeNewDimension:
    """The IDimension behind a new display dimension; SystemValue starts at the measured geometry.

    `rejects` models a value the solver does not take (or a driven dimension): SetSystemValue3 stores nothing.
    `unreadable` makes every read of SystemValue raise. `driven_state` is DrivenState (2 driving, 1 driven,
    0 unknown); an exception instance makes it unreadable.
    """

    def __init__(self, system_value: float, rejects: bool = False, unreadable: bool = False,
                 driven_state: object = 2) -> None:
        self._value = system_value
        self._rejects = rejects
        self._unreadable = unreadable
        self._driven_state = driven_state
        self.FullName = "D3@Skizze1@Part1.Part"
        self.set_calls: list[float] = []

    def _FlagAsMethod(self, *names: str) -> None:
        pass

    @property
    def DrivenState(self) -> object:  # noqa: N802 - COM member name
        if isinstance(self._driven_state, Exception):
            raise self._driven_state
        return self._driven_state

    @property
    def SystemValue(self) -> float:  # noqa: N802 - COM member name
        if self._unreadable:
            raise RuntimeError("SystemValue cannot be read")
        return self._value

    def SetSystemValue3(self, target: float, scope: int, config) -> int:  # noqa: N802 - COM member name
        self.set_calls.append(target)
        if not self._rejects:
            self._value = target
        return 0


class FakeDisplayDimension:
    """IDisplayDimension: GetDimension2 hands back `dimension`, or raises when it is None (GetDimension then too)."""

    def __init__(self, dimension: FakeNewDimension | None) -> None:
        self._dimension = dimension

    def _FlagAsMethod(self, *names: str) -> None:
        pass

    def GetDimension2(self, index: int):  # noqa: N802 - COM member name
        if self._dimension is None:
            raise RuntimeError("no dimension")
        return self._dimension

    def GetDimension(self):  # noqa: N802 - COM member name
        return self._dimension


class FakeDimensionDoc:
    """IModelDoc2 for add_dimension: AddDimension2 and its kin return `display` and set the open sketch's state."""

    def __init__(self, display, sketch: FakeSketch | None, code_after: int | None = None, rebuilt: bool = True,
                 on_rebuild=None) -> None:
        self._display = display
        self._sketch = sketch
        self._code_after = code_after
        self._rebuilt = rebuilt
        self._on_rebuild = on_rebuild
        self.SketchManager = FakeSketchManager(sketch)
        self.rebuilds = 0

    def _FlagAsMethod(self, *names: str) -> None:
        pass

    def _add(self, x: float, y: float, z: float):
        if self._sketch is not None and self._code_after is not None:
            self._sketch.code = self._code_after
        return self._display

    AddDimension2 = AddHorizontalDimension2 = AddVerticalDimension2 = _add  # noqa: N815 - COM member names
    AddRadialDimension2 = AddDiameterDimension2 = _add  # noqa: N815 - COM member names

    def ClearSelection2(self, all_: bool) -> bool:  # noqa: N802 - COM member name
        return True

    def EditRebuild3(self) -> bool:  # noqa: N802 - COM member name
        self.rebuilds += 1
        if self._on_rebuild:
            self._on_rebuild()
        return self._rebuilt


class AddDimensionTests(unittest.TestCase):
    """add_dimension judges the new dimension by readback and the open sketch's solve state (SW_MCP#15)."""

    def call(self, doc: FakeDimensionDoc, **args) -> dict:
        with unittest.mock.patch.object(sw_sketch, "active_document", return_value=(None, doc)), \
                unittest.mock.patch.object(sw_sketch, "require_selection", return_value=1), \
                unittest.mock.patch.object(sw_sketch, "empty_variant", return_value=None):
            return sw_sketch.add_dimension({"selection": {"sketch_segments": [0]}, "kind": "horizontal", **args})

    def doc(self, before: int, after: int | None, dimension: FakeNewDimension | None = None) -> FakeDimensionDoc:
        return FakeDimensionDoc(FakeDisplayDimension(dimension), FakeSketch(before), after)

    def test_value_applied_in_a_solving_sketch(self) -> None:
        for after, name in ((UNDER, "under_defined"), (FULLY, "fully_defined")):
            with self.subTest(after=name):
                dimension = FakeNewDimension(0.080)
                answer = self.call(self.doc(UNDER, after, dimension), value_mm=90)
                self.assertTrue(answer["ok"], answer)
                self.assertEqual(answer["data"]["sketch_status"], name)
                self.assertEqual(answer["data"]["sketch_status_before"], "under_defined")
                self.assertTrue(answer["data"]["applied"])
                self.assertAlmostEqual(answer["data"]["actual_value_mm"], 90.0)
                self.assertEqual(answer["data"]["full_name"], "D3@Skizze1@Part1.Part")
                self.assertIs(answer["data"]["driven"], False)
                self.assertEqual(answer["data"]["driven_state"], 2)
                self.assertEqual(dimension.set_calls, [0.090])

    def test_new_dimension_leaving_the_sketch_unsolved_fails(self) -> None:
        for after, name in ((OVER, "over_defined"), (NO_SOLUTION, "no_solution"), (INVALID, "invalid_solution")):
            with self.subTest(after=name):
                answer = self.call(self.doc(UNDER, after, FakeNewDimension(0.080)), value_mm=80)
                self.assertFalse(answer["ok"])
                self.assertEqual(answer["data"]["sketch_status"], name)
                self.assertIn(name, answer["message"])
                self.assertIn("D3@Skizze1@Part1.Part", answer["message"])
                self.assertIn("did not move", answer["message"])
                self.assertIn("stays in the sketch", answer["message"])

    def test_unsolved_sketch_fails_even_when_the_value_reads_back(self) -> None:
        answer = self.call(self.doc(UNDER, OVER, FakeNewDimension(0.080)), value_mm=70)
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["data"]["applied"])

    def test_sketch_already_unsolved_before_the_call(self) -> None:
        answer = self.call(self.doc(OVER, OVER, FakeNewDimension(0.050)), value_mm=50)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["sketch_status_before"], "over_defined")
        self.assertIn("already over_defined", answer["message"])

    def test_value_not_taken_fails(self) -> None:
        answer = self.call(self.doc(UNDER, UNDER, FakeNewDimension(0.080, rejects=True)), value_mm=70)
        self.assertFalse(answer["ok"])
        self.assertFalse(answer["data"]["applied"])
        self.assertAlmostEqual(answer["data"]["actual_value_mm"], 80.0)
        self.assertIn("did not take 70.0 mm", answer["message"])

    def test_unreadable_value_is_not_taken(self) -> None:
        # The readback used to fall back to the requested value, so this answered ok with applied true.
        answer = self.call(self.doc(UNDER, UNDER, FakeNewDimension(0.080, unreadable=True)), value_mm=70)
        self.assertFalse(answer["ok"])
        self.assertFalse(answer["data"]["applied"])
        self.assertIsNone(answer["data"]["actual_value_mm"])
        self.assertIn("could not be read back", answer["message"])
        self.assertNotIn("None", answer["message"])

    def test_value_not_taken_names_a_nonzero_set_status(self) -> None:
        dimension = FakeNewDimension(0.080, rejects=True)
        dimension.SetSystemValue3 = lambda target, scope, config: 7
        answer = self.call(self.doc(UNDER, UNDER, dimension), value_mm=70)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["set_value_status"], 7)
        self.assertIn("status 7", answer["message"])

    def test_dimension_not_handed_back_with_a_value_fails(self) -> None:
        answer = self.call(self.doc(UNDER, UNDER, None), value_mm=70)
        self.assertFalse(answer["ok"])
        self.assertFalse(answer["data"]["applied"])
        self.assertIn("did not hand it back", answer["message"])

    def test_dimension_not_handed_back_without_a_value_is_not_confirmed_driving(self) -> None:
        # This answered ok true before SW_MCP#22: without the IDimension nothing says the dimension drives.
        answer = self.call(self.doc(UNDER, FULLY, None))
        self.assertFalse(answer["ok"], answer)
        self.assertIs(answer["data"]["verified"], False)
        self.assertIsNone(answer["data"]["driven"])
        self.assertNotIn("driven_state", answer["data"])
        self.assertNotIn("applied", answer["data"])
        self.assertIn("did not hand the dimension back", answer["message"])

    def test_redundant_dimension_created_driven_fails_although_the_value_reads_back(self) -> None:
        # Case (c) of SW_MCP#22: 80 on a line already dimensioned 80, SetSystemValue3 status 3.
        dimension = FakeNewDimension(0.080, driven_state=1)
        dimension.SetSystemValue3 = lambda target, scope, config: (setattr(dimension, "_value", target), 3)[1]
        answer = self.call(self.doc(UNDER, UNDER, dimension), value_mm=80)
        self.assertFalse(answer["ok"], answer)
        self.assertIs(answer["data"]["driven"], True)
        self.assertEqual(answer["data"]["driven_state"], 1)
        self.assertTrue(answer["data"]["applied"])
        self.assertNotIn("verified", answer["data"])
        for part in ("D3@Skizze1@Part1.Part", "driven (reference) dimension", "may duplicate",
                     "reads the requested 80.0 mm", "status 3", "stays in the sketch as a reference dimension"):
            self.assertIn(part, answer["message"])

    def test_conflicting_dimension_created_driven_names_the_driven_state_before_the_value(self) -> None:
        # Case (b) of SW_MCP#22: 70 on a line already dimensioned 80, the value stays 80.
        answer = self.call(self.doc(UNDER, UNDER, FakeNewDimension(0.080, rejects=True, driven_state=1)), value_mm=70)
        self.assertFalse(answer["ok"])
        self.assertIs(answer["data"]["driven"], True)
        self.assertFalse(answer["data"]["applied"])
        self.assertIn("reference dimension", answer["message"])
        self.assertIn("reads 80.0 mm, not the requested 70.0 mm", answer["message"])

    def test_driven_dimension_with_an_unreadable_value_says_so(self) -> None:
        answer = self.call(self.doc(UNDER, UNDER, FakeNewDimension(0.080, unreadable=True, driven_state=1)),
                           value_mm=70)
        self.assertFalse(answer["ok"])
        self.assertIs(answer["data"]["driven"], True)
        self.assertIn("could not be read back after setting 70.0 mm", answer["message"])
        self.assertNotIn("None", answer["message"])

    def test_driven_dimension_without_a_requested_value_fails(self) -> None:
        # As _anchor_sketch calls it: a driven location dimension is not the driving one asked for (decision 1).
        answer = self.call(self.doc(UNDER, UNDER, FakeNewDimension(0.080, driven_state=1)))
        self.assertFalse(answer["ok"])
        self.assertIs(answer["data"]["driven"], True)
        self.assertNotIn("applied", answer["data"])
        self.assertIn("reference dimension", answer["message"])
        self.assertNotIn("requested", answer["message"])

    def test_driven_dimension_with_no_sketch_open_fails_after_the_rebuild(self) -> None:
        doc = FakeDimensionDoc(FakeDisplayDimension(FakeNewDimension(0.080, driven_state=1)), None)
        answer = self.call(doc, value_mm=80)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(doc.rebuilds, 1)
        self.assertIs(answer["data"]["driven"], True)

    def test_driven_state_is_read_after_the_rebuild(self) -> None:
        dimension = FakeNewDimension(0.080)
        doc = FakeDimensionDoc(FakeDisplayDimension(dimension), None,
                               on_rebuild=lambda: setattr(dimension, "_driven_state", 1))
        answer = self.call(doc, value_mm=70)
        self.assertFalse(answer["ok"], answer)
        self.assertIs(answer["data"]["driven"], True)

    def test_unknown_driven_state_is_not_confirmed(self) -> None:
        for state in (0, 5):
            with self.subTest(state=state):
                answer = self.call(self.doc(UNDER, UNDER, FakeNewDimension(0.080, driven_state=state)), value_mm=70)
                self.assertFalse(answer["ok"])
                self.assertIs(answer["data"]["verified"], False)
                self.assertIsNone(answer["data"]["driven"])
                self.assertEqual(answer["data"]["driven_state"], state)
                self.assertTrue(answer["data"]["applied"])
                self.assertIn(f"DrivenState reads {state}", answer["message"])
                self.assertIn("not confirmed", answer["message"])

    def test_unreadable_driven_state_is_not_confirmed(self) -> None:
        for raw in (RuntimeError("DrivenState cannot be read"), None, True, "x"):
            with self.subTest(raw=repr(raw)):
                answer = self.call(self.doc(UNDER, UNDER, FakeNewDimension(0.080, driven_state=raw)), value_mm=70)
                self.assertFalse(answer["ok"])
                self.assertIs(answer["data"]["verified"], False)
                self.assertIsNone(answer["data"]["driven"])
                self.assertIsNone(answer["data"]["driven_state"])
                self.assertIn("DrivenState cannot be read", answer["message"])

    def test_unconfirmed_driven_state_marks_every_failing_rule_unverified(self) -> None:
        # Each earlier rule keeps its message, but driven null always carries verified false (decision 2A).
        cases = {
            "value rejected, state 0": (self.doc(UNDER, UNDER, FakeNewDimension(0.080, rejects=True, driven_state=0)),
                                        "did not take 70.0 mm"),
            "value rejected, read error": (
                self.doc(UNDER, UNDER, FakeNewDimension(0.080, rejects=True,
                                                        driven_state=RuntimeError("DrivenState cannot be read"))),
                "did not take 70.0 mm"),
            "no IDimension, value": (self.doc(UNDER, UNDER, None), "did not hand it back"),
            "unsolved sketch, state 0": (self.doc(UNDER, OVER, FakeNewDimension(0.080, driven_state=0)), "did not move"),
            "unreadable sketch, state 0": (self.doc(UNDER, 1, FakeNewDimension(0.080, driven_state=0)),
                                           "not confirmed"),
        }
        for label, (doc, phrase) in cases.items():
            with self.subTest(case=label):
                answer = self.call(doc, value_mm=70)
                self.assertFalse(answer["ok"])
                self.assertIs(answer["data"]["verified"], False)
                self.assertIsNone(answer["data"]["driven"])
                self.assertIn(phrase, answer["message"])

    def test_unsolved_sketch_message_wins_over_a_driven_dimension(self) -> None:
        answer = self.call(self.doc(UNDER, OVER, FakeNewDimension(0.080, driven_state=1)), value_mm=80)
        self.assertFalse(answer["ok"])
        self.assertIs(answer["data"]["driven"], True)
        self.assertIn("did not move", answer["message"])
        self.assertNotIn("reference dimension", answer["message"])

    def test_no_value_requested_and_sketch_over_defined_fails(self) -> None:
        answer = self.call(self.doc(UNDER, OVER, FakeNewDimension(0.080)))
        self.assertFalse(answer["ok"])
        self.assertNotIn("applied", answer["data"])

    def test_no_display_dimension_fails_as_before(self) -> None:
        answer = self.call(FakeDimensionDoc(None, FakeSketch(UNDER)), value_mm=70)
        self.assertFalse(answer["ok"])
        self.assertIn("did not create a dimension", answer["message"])

    def test_no_sketch_open_rebuilds_and_the_readback_decides(self) -> None:
        for rejects, ok in ((False, True), (True, False)):
            with self.subTest(rejects=rejects):
                doc = FakeDimensionDoc(FakeDisplayDimension(FakeNewDimension(0.080, rejects=rejects)), None)
                answer = self.call(doc, value_mm=70)
                self.assertEqual(answer["ok"], ok, answer)
                self.assertEqual(doc.rebuilds, 1)
                self.assertEqual(answer["data"]["sketch_status"], "closed")
                self.assertNotIn("verified", answer["data"])

    def test_no_sketch_open_value_reverting_at_the_rebuild_fails(self) -> None:
        # The measured #14 fault: SystemValue reads the new value until the rebuild, then the old one.
        dimension = FakeNewDimension(0.080)
        doc = FakeDimensionDoc(FakeDisplayDimension(dimension), None,
                               on_rebuild=lambda: setattr(dimension, "_value", 0.080))
        answer = self.call(doc, value_mm=70)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(doc.rebuilds, 1)
        self.assertFalse(answer["data"]["applied"])
        self.assertAlmostEqual(answer["data"]["actual_value_mm"], 80.0)
        self.assertIn("did not take 70.0 mm", answer["message"])

    def test_no_sketch_open_unreadable_value_after_the_rebuild_fails(self) -> None:
        dimension = FakeNewDimension(0.080)
        doc = FakeDimensionDoc(FakeDisplayDimension(dimension), None,
                               on_rebuild=lambda: setattr(dimension, "_unreadable", True))
        answer = self.call(doc, value_mm=70)
        self.assertFalse(answer["ok"], answer)
        self.assertIsNone(answer["data"]["actual_value_mm"])

    def test_no_sketch_open_failed_rebuild_fails(self) -> None:
        doc = FakeDimensionDoc(FakeDisplayDimension(FakeNewDimension(0.080)), None, rebuilt=False)
        answer = self.call(doc, value_mm=70)
        self.assertFalse(answer["ok"], answer)
        self.assertIs(answer["data"]["rebuilt"], False)
        self.assertIn("rebuild reported a problem", answer["message"])

    def test_open_sketch_is_not_rebuilt(self) -> None:
        doc = self.doc(UNDER, UNDER, FakeNewDimension(0.080))
        answer = self.call(doc, value_mm=70)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(doc.rebuilds, 0)
        self.assertNotIn("rebuilt", answer["data"])

    def test_nonzero_set_status_with_a_matching_readback_is_ok(self) -> None:
        dimension = FakeNewDimension(0.080)
        dimension.SetSystemValue3 = lambda target, scope, config: (setattr(dimension, "_value", target), 3)[1]
        answer = self.call(self.doc(UNDER, UNDER, dimension), value_mm=70)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["data"]["set_value_status"], 3)

    def test_angular_dimension_reports_degrees(self) -> None:
        answer = self.call(self.doc(UNDER, UNDER, FakeNewDimension(math.radians(30))), value_deg=45)
        self.assertTrue(answer["ok"], answer)
        self.assertAlmostEqual(answer["data"]["actual_value_deg"], 45.0)

    def test_unreadable_sketch_state_is_not_a_success(self) -> None:
        for after in (1, 99):   # 1 reads "unknown", 99 is outside the table
            with self.subTest(code=after):
                answer = self.call(self.doc(UNDER, after, FakeNewDimension(0.080)), value_mm=70)
                self.assertFalse(answer["ok"])
                self.assertIs(answer["data"]["verified"], False)
                self.assertTrue(answer["data"]["applied"])
                self.assertIn("not confirmed", answer["message"])


class FakeDiametricDisplay(FakeDisplayDimension):
    """IDisplayDimension with Diametric as measured on 2016 SP3 (#75): with `takes` false a write is ignored and the
    property reads back what it was; `set_error` / `read_error` make the write or the read raise; `reads` replaces the
    value read back (a non-bool, say)."""

    def __init__(self, dimension: FakeNewDimension | None, takes: bool = True, initial: bool = False,
                 set_error: Exception | None = None, read_error: Exception | None = None, reads: object = ...,
                 selection_name: str = "D3@Skizze1") -> None:
        super().__init__(dimension)
        self._takes, self._state = takes, initial
        self._set_error, self._read_error, self._reads = set_error, read_error, reads
        self.selection_name = selection_name

    @property
    def Diametric(self) -> object:  # noqa: N802 - COM member name
        if self._read_error is not None:
            raise self._read_error
        return self._state if self._reads is ... else self._reads

    @Diametric.setter
    def Diametric(self, wanted: bool) -> None:  # noqa: N802 - COM member name
        if self._set_error is not None:
            raise self._set_error
        if self._takes:
            self._state = bool(wanted)

    def GetNameForSelection(self) -> str:  # noqa: N802 - COM member name
        return self.selection_name


class FakeExtension:
    """IModelDocExtension: SelectByID2 selects the display by its selection name (unless `selects` is false),
    DeleteSelection2 removes the selected dimension from the document (unless `deletes` is false)."""

    def __init__(self, doc: "FakeDiametricDoc", selects: bool, deletes: bool) -> None:
        self._doc, self._selects, self._deletes = doc, selects, deletes
        self.selected: list[tuple[str, str]] = []
        self.delete_calls = 0

    def _FlagAsMethod(self, *names: str) -> None:
        pass

    def SelectByID2(self, name, kind, x, y, z, append, mark, callout, option) -> bool:  # noqa: N802 - COM member name
        self.selected.append((name, kind))
        return self._selects and kind == "DIMENSION" and name == self._doc._display.selection_name

    def DeleteSelection2(self, options: int) -> bool:  # noqa: N802 - COM member name
        self.delete_calls += 1
        if self._deletes and self.selected:
            self._doc.present.clear()
        return True   # measured elsewhere: the answer says True also for what stays (sw_drawing._delete_selected)


class FakeDiametricDoc(FakeDimensionDoc):
    """FakeDimensionDoc plus Extension and Parameter: the new dimension resolves by full name until it is deleted."""

    def __init__(self, display: FakeDiametricDisplay, sketch: FakeSketch | None, selects: bool = True,
                 deletes: bool = True) -> None:
        super().__init__(display, sketch)
        self.Extension = FakeExtension(self, selects, deletes)
        dimension = display._dimension
        self.present = {dimension.FullName} if dimension is not None else set()

    def Parameter(self, name: str):  # noqa: N802 - COM member name
        return self._display._dimension if name in self.present else None


class AddDimensionDiametricTests(unittest.TestCase):
    """add_dimension with diametric: a refused diameter display sets no value and removes the dimension (#75)."""

    def call(self, doc: FakeDiametricDoc, **args) -> dict:
        with unittest.mock.patch.object(sw_sketch, "active_document", return_value=(None, doc)), \
                unittest.mock.patch.object(sw_sketch, "require_selection", return_value=2), \
                unittest.mock.patch.object(sw_sketch, "empty_variant", return_value=None), \
                unittest.mock.patch.object(sw_sketch, "nothing", return_value=None):
            return sw_sketch.add_dimension({"selection": {"sketch_segments": [1, 0]}, "diametric": True, **args})

    def refused(self, display: FakeDiametricDisplay, **doc_args) -> tuple[FakeDiametricDoc, FakeNewDimension]:
        return FakeDiametricDoc(display, FakeSketch(UNDER), **doc_args), display._dimension

    def test_refused_display_sets_no_value_and_removes_the_dimension(self) -> None:
        # The four ways of probe_durchmesser.py differ in selection order, text position and kind; on 2016 every
        # one read Diametric false and moved the line to 2·y. Offline the kind is the one input that reaches the code.
        for kind in ("auto", "vertical", "horizontal"):
            with self.subTest(kind=kind):
                doc, dimension = self.refused(FakeDiametricDisplay(FakeNewDimension(0.010), takes=False))
                answer = self.call(doc, kind=kind, value_mm=20)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(dimension.set_calls, [])
                data = answer["data"]
                self.assertIs(data["diametric"], False)
                self.assertIs(data["removed"], True)
                self.assertEqual(data["full_name"], "D3@Skizze1@Part1.Part")
                self.assertEqual(data["sketch_status_before"], "under_defined")
                self.assertEqual(doc.Extension.selected, [("D3@Skizze1", "DIMENSION")])
                self.assertEqual(doc.Extension.delete_calls, 1)
                self.assertEqual(doc.rebuilds, 0)
                for part in ("did not take the diameter display", "Diametric reads False", "none was set",
                             "D3@Skizze1@Part1.Part was removed again"):
                    self.assertIn(part, answer["message"])

    def test_refused_display_without_a_value_also_fails_and_removes(self) -> None:
        doc, dimension = self.refused(FakeDiametricDisplay(FakeNewDimension(0.010), takes=False))
        answer = self.call(doc)
        self.assertFalse(answer["ok"], answer)
        self.assertIs(answer["data"]["removed"], True)
        self.assertEqual(dimension.set_calls, [])

    def test_unreadable_or_raising_display_counts_as_refused(self) -> None:
        cases = {
            "write raises": (FakeDiametricDisplay(FakeNewDimension(0.010), set_error=RuntimeError("COM")),
                             "setting Diametric raised RuntimeError"),
            "read raises": (FakeDiametricDisplay(FakeNewDimension(0.010), read_error=RuntimeError("COM")),
                            "Diametric cannot be read back"),
            "reads None": (FakeDiametricDisplay(FakeNewDimension(0.010), reads=None), "Diametric reads None"),
            "reads 1": (FakeDiametricDisplay(FakeNewDimension(0.010), reads=1), "Diametric reads 1"),
        }
        for label, (display, why) in cases.items():
            with self.subTest(case=label):
                doc, dimension = self.refused(display)
                answer = self.call(doc, value_mm=20)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(dimension.set_calls, [])
                self.assertIs(answer["data"]["diametric"], False)
                self.assertIs(answer["data"]["removed"], True)
                self.assertIn(why, answer["message"])

    def test_failed_removal_says_the_dimension_stays_and_names_it(self) -> None:
        for label, doc_args in (("not selected", {"selects": False}), ("not deleted", {"deletes": False})):
            with self.subTest(case=label):
                doc, dimension = self.refused(FakeDiametricDisplay(FakeNewDimension(0.010), takes=False), **doc_args)
                answer = self.call(doc, value_mm=20)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(dimension.set_calls, [])
                self.assertIs(answer["data"]["removed"], False)
                self.assertEqual(answer["data"]["full_name"], "D3@Skizze1@Part1.Part")
                self.assertIn("D3@Skizze1@Part1.Part could not be removed and stays in the sketch", answer["message"])

    def test_raising_removal_is_reported_as_not_removed(self) -> None:
        doc, dimension = self.refused(FakeDiametricDisplay(FakeNewDimension(0.010), takes=False))

        def broken(options: int) -> bool:
            raise RuntimeError("COM")
        doc.Extension.DeleteSelection2 = broken
        answer = self.call(doc, value_mm=20)
        self.assertFalse(answer["ok"], answer)
        self.assertIs(answer["data"]["removed"], False)
        self.assertEqual(dimension.set_calls, [])

    def test_refused_display_without_its_dimension_cannot_confirm_a_removal(self) -> None:
        doc, _ = self.refused(FakeDiametricDisplay(None, takes=False))
        answer = self.call(doc, value_mm=20)
        self.assertFalse(answer["ok"], answer)
        self.assertIs(answer["data"]["removed"], False)
        self.assertEqual(answer["data"]["full_name"], "")
        self.assertIn("could not be named or removed", answer["message"])

    def test_taken_display_sets_the_value_as_a_diameter(self) -> None:
        doc, dimension = self.refused(FakeDiametricDisplay(FakeNewDimension(0.010)))
        answer = self.call(doc, value_mm=20)
        self.assertTrue(answer["ok"], answer)
        self.assertIs(answer["data"]["diametric"], True)
        self.assertNotIn("removed", answer["data"])
        self.assertEqual(dimension.set_calls, [0.020])
        self.assertEqual(doc.Extension.delete_calls, 0)


class SetDimensionDiametricTests(unittest.TestCase):
    """set_dimension with diametric: a display that does not switch is ok false and no value is written (#75)."""

    NAME = SetDimensionTests.NAME

    def call(self, doc: FakeDoc, display: FakeDiametricDisplay, **args) -> dict:
        with unittest.mock.patch.object(sw_sketch, "active_document", return_value=(None, doc)), \
                unittest.mock.patch.object(sw_sketch, "empty_variant", return_value=None), \
                unittest.mock.patch.object(sw_sketch, "_display_dimension", return_value=display):
            return sw_sketch.set_dimension({"full_name": self.NAME, **args})

    def case(self, active: bool) -> tuple[FakeDoc, FakeDimension]:
        sketch = FakeSketch(FULLY)
        dimension = FakeDimension(0.025, FakeOwner("P_Skizze", sw_core.SKETCH_2D_TYPE, sketch))
        return FakeDoc(dimension, active_sketch=sketch if active else None), dimension

    def test_display_only_that_does_not_switch_fails_without_a_rebuild(self) -> None:
        for active in (False, True):
            with self.subTest(sketch_open=active):
                doc, dimension = self.case(active)
                answer = self.call(doc, FakeDiametricDisplay(None, takes=False), diametric=True)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(dimension.set_calls, [])
                self.assertEqual(doc.rebuilds, 0)
                self.assertIs(answer["data"]["diametric"], False)
                self.assertIn("did not switch", answer["message"])
                self.assertIn("diameter display", answer["message"])

    def test_value_with_a_refused_display_is_never_written(self) -> None:
        for active in (False, True):
            with self.subTest(sketch_open=active):
                doc, dimension = self.case(active)
                answer = self.call(doc, FakeDiametricDisplay(None, takes=False), diametric=True, value_mm=50)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(dimension.set_calls, [])
                self.assertEqual(doc.rebuilds, 0)
                self.assertIn("50.0 mm was not set", answer["message"])
                self.assertEqual(answer["data"]["value_mm"], 50.0)

    def test_switch_back_to_the_distance_is_checked_too(self) -> None:
        doc, dimension = self.case(False)
        answer = self.call(doc, FakeDiametricDisplay(None, takes=False, initial=True), diametric=False, value_mm=25)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(dimension.set_calls, [])
        self.assertIs(answer["data"]["diametric"], True)
        self.assertIn("plain distance display", answer["message"])

    def test_raising_or_unreadable_display_fails_without_a_write(self) -> None:
        for label, display in (("write raises", FakeDiametricDisplay(None, set_error=RuntimeError("COM"))),
                               ("read raises", FakeDiametricDisplay(None, read_error=RuntimeError("COM"))),
                               ("reads None", FakeDiametricDisplay(None, reads=None))):
            with self.subTest(case=label):
                doc, dimension = self.case(False)
                answer = self.call(doc, display, diametric=True, value_mm=50)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(dimension.set_calls, [])
                self.assertEqual(doc.rebuilds, 0)

    def test_switched_display_then_writes_the_value(self) -> None:
        doc, dimension = self.case(False)
        answer = self.call(doc, FakeDiametricDisplay(None), diametric=True, value_mm=50)
        self.assertTrue(answer["ok"], answer)
        self.assertIs(answer["data"]["diametric"], True)
        self.assertEqual(dimension.set_calls, [0.050])


class FakeLine:
    """A sketch line for _anchor_sketch: GetType 0 (swSketchLINE) and its ends in metres."""

    def __init__(self, start: tuple[float, float], end: tuple[float, float]) -> None:
        self._start, self._end = start, end

    def GetType(self) -> int:  # noqa: N802 - COM member name
        return 0

    def GetStartPoint2(self):  # noqa: N802 - COM member name
        return unittest.mock.Mock(X=self._start[0], Y=self._start[1])

    def GetEndPoint2(self):  # noqa: N802 - COM member name
        return unittest.mock.Mock(X=self._end[0], Y=self._end[1])


class FakeRelationManager:
    def _FlagAsMethod(self, *names: str) -> None:
        pass

    def AddRelation(self, entities, code: int) -> bool:  # noqa: N802 - COM member name
        return True


class AnchorSketchTests(unittest.TestCase):
    """fully_define_sketch lists the location dimensions add_dimension created, also those it judged a failure."""

    def test_location_dimensions_count_what_add_dimension_created(self) -> None:
        sketch = unittest.mock.Mock(RelationManager=FakeRelationManager())
        doc = unittest.mock.Mock(SketchManager=FakeSketchManager(sketch))
        lines = [FakeLine((0.01, 0.02), (0.05, 0.02)), FakeLine((0.03, 0.0), (0.03, 0.04))]
        answers = [
            # Left in the sketch, judged a failure (#15): still a location dimension of this sketch.
            {"ok": False, "message": "left over_defined", "data": {"full_name": "D1@S", "sketch_status": "over_defined"}},
            {"ok": True, "message": "Added a dimension.", "data": {"full_name": "D2@S", "sketch_status": "fully_defined"}},
        ]
        with unittest.mock.patch.object(sw_sketch, "origin_sketch_point", return_value=object()), \
                unittest.mock.patch.object(sw_sketch, "sketch_point_objects", return_value=[]), \
                unittest.mock.patch.object(sw_sketch, "sketch_segment_objects", return_value=lines), \
                unittest.mock.patch.object(sw_sketch, "dispatch_array", side_effect=lambda items: items), \
                unittest.mock.patch.object(sw_sketch, "add_dimension", side_effect=answers) as add:
            anchored = sw_sketch._anchor_sketch(doc)
        self.assertEqual(add.call_count, 2)
        self.assertEqual(anchored["location_dimensions"], ["D1@S", "D2@S"])

    def test_note_created_skips_a_dimension_that_was_never_created(self) -> None:
        dimensions: list[str] = []
        sw_sketch._note_created(dimensions, {"ok": False, "message": "did not create", "data": {"entities": 1}}, "vertical")
        sw_sketch._note_created(dimensions, {"ok": True, "message": "Added a dimension.", "data": {}}, "vertical")
        self.assertEqual(dimensions, ["vertical"])


class SketchStatusTests(unittest.TestCase):
    def test_codes_map_to_names_and_errors_read_unknown(self) -> None:
        self.assertEqual(sw_core.sketch_status(FakeSketch(NO_SOLUTION)), "no_solution")
        self.assertEqual(sw_core.sketch_status(FakeSketch(FULLY)), "fully_defined")
        self.assertEqual(sw_core.sketch_status(FakeSketch(99)), "status_99")
        self.assertEqual(sw_core.sketch_status(object()), "unknown")
        self.assertTrue(sw_core.unreadable_sketch_state("unknown"))
        self.assertTrue(sw_core.unreadable_sketch_state("status_99"))
        self.assertFalse(sw_core.unreadable_sketch_state("under_defined"))
        self.assertFalse(sw_core.unreadable_sketch_state("no_solution"))

    def test_open_sketch_status_reports_closed_without_a_sketch(self) -> None:
        self.assertEqual(sw_sketch._sketch_status(FakeDoc(FakeDimension(0.0, None))), "closed")
        self.assertEqual(sw_sketch._sketch_status(FakeDoc(FakeDimension(0.0, None), active_sketch=FakeSketch(OVER))),
                         "over_defined")

    def test_sketch_states_reads_only_sketch_features_and_marks_unreadable_ones(self) -> None:
        good = FakeOwner("Skizze1", sw_core.SKETCH_2D_TYPE, FakeSketch(FULLY))
        bad = FakeOwner("3DSkizze1", sw_core.SKETCH_3D_TYPE, FakeSketch(NO_SOLUTION))
        broken = FakeOwner("Skizze2", sw_core.SKETCH_2D_TYPE, None)
        extrude = FakeOwner("Boss-Extrude1", "Extrusion", FakeSketch(NO_SOLUTION))
        with unittest.mock.patch.object(sw_core, "iter_feature_objects", return_value=[good, extrude, bad, broken]):
            states = sw_core.sketch_states(object())
            unsolved = sw_core.unsolved_sketches(object())
        self.assertEqual(states, [{"sketch": "Skizze1", "sketch_status": "fully_defined"},
                                  {"sketch": "3DSkizze1", "sketch_status": "no_solution"},
                                  {"sketch": "Skizze2", "sketch_status": "unknown"}])
        self.assertEqual(unsolved, [{"sketch": "3DSkizze1", "sketch_status": "no_solution"}])


class FakeHolePoint:
    """An ISketchPoint: GetID [type, id] and its coordinates."""

    def __init__(self, ident: int, x: float, y: float) -> None:
        self.ident, self.X, self.Y = ident, x, y

    def GetID(self):  # noqa: N802 - COM member name
        return [1, self.ident]


class FakeHoleSegment:
    """A sketch circle or line; `refuses_construction` keeps ConstructionGeometry false."""

    def __init__(self, ident: int, points: dict[str, FakeHolePoint], refuses_construction: bool = False) -> None:
        self.ident, self._points, self._refuses = ident, points, refuses_construction
        self._construction = False

    @property
    def ConstructionGeometry(self) -> bool:  # noqa: N802 - COM member name
        return self._construction

    @ConstructionGeometry.setter
    def ConstructionGeometry(self, flag: bool) -> None:  # noqa: N802 - COM member name
        if not self._refuses:
            self._construction = bool(flag)

    def GetID(self):  # noqa: N802 - COM member name
        return [2, self.ident]

    def GetCenterPoint2(self):  # noqa: N802 - COM member name
        return self._points["centre"]

    def GetStartPoint2(self):  # noqa: N802 - COM member name
        return self._points["start"]

    def GetEndPoint2(self):  # noqa: N802 - COM member name
        return self._points["end"]


class FakeHoleRelations:
    """ISketchRelationManager: AddRelation counts one relation unless its code is in `refuses`."""

    def __init__(self, refuses: set[int]) -> None:
        self.count, self._refuses = 0, refuses
        self.added: list[tuple[int, int]] = []

    def _FlagAsMethod(self, *names: str) -> None:
        pass

    def GetRelationsCount(self, kind: int) -> int:  # noqa: N802 - COM member name
        return self.count

    def AddRelation(self, entities, code: int) -> bool:  # noqa: N802 - COM member name
        self.added.append((code, len(entities)))
        if code not in self._refuses:
            self.count += 1
        return True


class FakeHoleSketch:
    """The open ISketch and its ISketchManager in one: Create* merge a point drawn on an existing point's exact
    coordinates into that point (measured on 2016 SP3, ClauSW#78 probe B1) unless `merges` is false."""

    def __init__(self, status: int = FULLY, merges: bool = True, missing: str = "", refuses: set[int] = frozenset(),
                 refuses_construction: bool = False, segments: list | None = None) -> None:
        self.code, self._merges, self._missing = status, merges, missing
        self._refuses_construction = refuses_construction
        self.segments: list[FakeHoleSegment] = list(segments or [])
        self.points: list[FakeHolePoint] = []
        self.RelationManager = FakeHoleRelations(set(refuses))
        self.AddToDB = False
        self.created: list[str] = []

    # ISketchManager
    ActiveSketch = property(lambda self: self)

    def _FlagAsMethod(self, *names: str) -> None:
        pass

    def _point(self, x: float, y: float) -> FakeHolePoint:
        for point in self.points:
            if self._merges and point.X == x and point.Y == y:
                return point
        point = FakeHolePoint(len(self.points) + 1, x, y)
        self.points.append(point)
        return point

    def _segment(self, what: str, points: dict[str, FakeHolePoint]) -> FakeHoleSegment | None:
        self.created.append(what)
        if self._missing == what:
            return None
        segment = FakeHoleSegment(len(self.segments) + 1, points, self._refuses_construction)
        self.segments.append(segment)
        return segment

    def CreateCircleByRadius(self, x, y, z, radius):  # noqa: N802 - COM member name
        return self._segment("circle", {"centre": self._point(x, y)})

    def CreateLine(self, x1, y1, z1, x2, y2, z2):  # noqa: N802 - COM member name
        return self._segment("line", {"start": self._point(x1, y1), "end": self._point(x2, y2)})

    # ISketch
    def GetSketchSegments(self):  # noqa: N802 - COM member name
        return list(self.segments)

    def GetConstrainedStatus(self) -> int:  # noqa: N802 - COM member name
        if isinstance(self.code, Exception):
            raise self.code
        return self.code


class FakeHoleDimension:
    """IDimension of a new dimension; `rejects`, `renames`, `driven_state` model the failures under test."""

    def __init__(self, number: int, rejects: bool = False, renames: bool = True, driven_state: object = 2) -> None:
        self.Name, self._rejects, self._renames, self._driven_state = f"D{number}", rejects, renames, driven_state
        self.SystemValue = 0.001
        self.set_calls: list[float] = []

    def __setattr__(self, key, value) -> None:
        if key == "Name" and getattr(self, "_renames", True) is False and "Name" in self.__dict__:
            return
        object.__setattr__(self, key, value)

    def _FlagAsMethod(self, *names: str) -> None:
        pass

    @property
    def FullName(self) -> str:  # noqa: N802 - COM member name
        return f"{self.Name}@Loecher_Skizze@Part1.Part"

    @property
    def DrivenState(self) -> object:  # noqa: N802 - COM member name
        if isinstance(self._driven_state, Exception):
            raise self._driven_state
        return self._driven_state

    def SetSystemValue3(self, target: float, scope: int, config) -> int:  # noqa: N802 - COM member name
        self.set_calls.append(target)
        if not self._rejects:
            self.SystemValue = target
        return 0


class FakeHoleDisplay:
    def __init__(self, dimension: FakeHoleDimension | None, type2: int) -> None:
        self._dimension, self.Type2 = dimension, type2

    def _FlagAsMethod(self, *names: str) -> None:
        pass

    def GetDimension2(self, index: int):  # noqa: N802 - COM member name
        return self._dimension


class FakeHoleDoc:
    """IModelDoc2 for draw_hole_circle. `faults` maps a dimension (pitch, hole, angle) to what goes wrong with it:
    no_display, no_dimension, wrong_type, rejects, no_rename, driven, driven_unreadable."""

    def __init__(self, sketch: FakeHoleSketch | None, faults: dict[str, str] | None = None) -> None:
        self.SketchManager = sketch if sketch is not None else FakeSketchManager(None)
        self._faults = faults or {}
        self.dimensions: list[FakeHoleDimension] = []
        self.order = ["pitch", "hole", "angle"]

    def _FlagAsMethod(self, *names: str) -> None:
        pass

    def ClearSelection2(self, all_: bool) -> bool:  # noqa: N802 - COM member name
        return True

    def _add(self, x: float, y: float, z: float, type2: int):
        key = self.order[len(self.dimensions)] if len(self.dimensions) < 3 else "extra"
        fault = self._faults.get(key, "")
        if fault == "no_display":
            return None
        dimension = FakeHoleDimension(len(self.dimensions) + 1, rejects=fault == "rejects",
                                      renames=fault != "no_rename",
                                      driven_state={"driven": 1, "driven_unreadable": RuntimeError("COM")}.get(fault, 2))
        self.dimensions.append(dimension)
        if fault == "wrong_type":
            type2 = 2
        return FakeHoleDisplay(None if fault == "no_dimension" else dimension, type2)

    def AddDiameterDimension2(self, x, y, z):  # noqa: N802 - COM member name
        return self._add(x, y, z, 6)

    def AddDimension2(self, x, y, z):  # noqa: N802 - COM member name
        return self._add(x, y, z, 3)


class DrawHoleCircleTests(unittest.TestCase):
    """draw_hole_circle (ClauSW#78): one call, fail-closed at the first step that does not hold."""

    def call(self, doc: FakeHoleDoc, extra_dimensions: int | None = 0, selects: bool = True, **args) -> dict:
        """extra_dimensions None makes the dimension count unreadable."""
        origin = FakeHolePoint(0, 0.0, 0.0)
        counted = lambda sketch: None if extra_dimensions is None else len(doc.dimensions) + extra_dimensions  # noqa: E731
        with unittest.mock.patch.object(sw_sketch, "active_document", return_value=(None, doc)), \
                unittest.mock.patch.object(sw_sketch, "origin_sketch_point", return_value=origin), \
                unittest.mock.patch.object(sw_sketch, "select_object", return_value=selects), \
                unittest.mock.patch.object(sw_sketch, "dispatch_array", side_effect=list), \
                unittest.mock.patch.object(sw_sketch, "empty_variant", return_value=None), \
                unittest.mock.patch.object(sw_sketch, "_sketch_dimension_count", side_effect=counted):
            return sw_sketch.draw_hole_circle({"pitch_diameter_mm": 108, "hole_diameter_mm": 14.5, **args})

    def test_angle_normalisation_and_quadrants(self) -> None:
        for given, wanted in ((-30, (330.0, None)), (360, (0.0, 0)), (450, (90.0, 1)), (270, (270.0, 3)),
                              (15, (15.0, None)), (90 - 1e-10, (90 - 1e-10, 1)), (195, (195.0, None))):
            with self.subTest(angle=given):
                phi, quadrant = sw_sketch._hole_circle_angle(given)
                self.assertAlmostEqual(phi, wanted[0])
                self.assertEqual(quadrant, wanted[1])

    def test_quadrant_angle_takes_a_relation_and_two_named_diameters(self) -> None:
        for angle, mode, code in ((90, "vertical", 5), (0, "horizontal", 4), (180, "horizontal", 4),
                                  (270, "vertical", 5), (450, "vertical", 5)):
            with self.subTest(angle=angle):
                sketch = FakeHoleSketch()
                doc = FakeHoleDoc(sketch)
                answer = self.call(doc, start_angle_deg=angle)
                self.assertTrue(answer["ok"], answer)
                data = answer["data"]
                self.assertEqual(data["angle_mode"], mode)
                self.assertIsNone(data["angle_measured_deg"])
                self.assertEqual(data["sketch_status"], "fully_defined")
                self.assertEqual([(d["full_name"], d["kind"], d["value_mm"], d["driven"]) for d in data["dimensions"]],
                                 [("Lochkreis@Loecher_Skizze@Part1.Part", "diameter", 108.0, False),
                                  ("Bohrung@Loecher_Skizze@Part1.Part", "diameter", 14.5, False)])
                self.assertEqual(sketch.created, ["circle", "circle", "line"])
                self.assertEqual(sketch.RelationManager.added, [(9, 2), (9, 2), (code, 1)])
                self.assertEqual(data["segments"], {"pitch": 0, "hole": 1, "radius": 2})
                self.assertEqual([s.ConstructionGeometry for s in sketch.segments], [True, False, True])
                self.assertEqual(len(sketch.points), 2)   # centre and hole centre, line ends merged

    def test_off_quadrant_angle_takes_an_angle_dimension_with_360_minus_phi_above_180(self) -> None:
        for angle, measured in ((15, 15.0), (105, 105.0), (195, 165.0), (285, 75.0), (-30, 30.0)):
            with self.subTest(angle=angle):
                sketch = FakeHoleSketch()
                doc = FakeHoleDoc(sketch)
                answer = self.call(doc, start_angle_deg=angle, names={"angle": "Winkel"})
                self.assertTrue(answer["ok"], answer)
                data = answer["data"]
                self.assertEqual(data["angle_mode"], "dimension")
                self.assertAlmostEqual(data["angle_measured_deg"], measured, places=9)
                self.assertEqual([d["name"] for d in data["dimensions"]], ["Lochkreis", "Bohrung", "Winkel"])
                self.assertEqual(data["dimensions"][2]["kind"], "angle")
                self.assertAlmostEqual(doc.dimensions[2].set_calls[0], math.radians(measured))
                self.assertEqual(sketch.created, ["circle", "circle", "line", "line"])
                self.assertEqual(sketch.RelationManager.added, [(9, 2), (9, 2), (4, 1), (9, 2)])
                self.assertEqual(data["segments"], {"pitch": 0, "hole": 1, "radius": 2, "reference": 3})
                self.assertEqual([s.ConstructionGeometry for s in sketch.segments], [True, False, True, True])
                self.assertEqual(len(sketch.points), 3)

    def assert_stopped(self, answer: dict, step: str) -> dict:
        self.assertFalse(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual(data["step"], step)
        self.assertIn(f"stopped at {step}", answer["message"])
        self.assertIn("stays open", answer["message"])
        return data

    def test_missing_segment_stops_before_the_next_segment(self) -> None:
        for missing, drawn, recorded in (("circle", ["circle"], []),
                                         ("line", ["circle", "circle", "line"], ["pitch", "hole"])):
            with self.subTest(missing=missing):
                sketch = FakeHoleSketch(missing=missing)
                data = self.assert_stopped(self.call(FakeHoleDoc(sketch), start_angle_deg=15), "geometry")
                self.assertEqual(sketch.created, drawn)   # no Create* call after the one that returned nothing
                self.assertEqual(data["created"]["segments"], recorded)
                self.assertEqual(sketch.RelationManager.added, [])
                self.assertEqual(data["created"]["dimensions"], [])

    def test_construction_flag_not_taken_stops(self) -> None:
        sketch = FakeHoleSketch(refuses_construction=True)
        self.assert_stopped(self.call(FakeHoleDoc(sketch)), "geometry")
        self.assertEqual(sketch.RelationManager.added, [])

    def test_line_end_not_merged_with_the_centre_stops(self) -> None:
        sketch = FakeHoleSketch(merges=False)
        data = self.assert_stopped(self.call(FakeHoleDoc(sketch)), "radius_start_on_centre")
        self.assertEqual(data["created"]["segments"], ["pitch", "hole", "radius"])
        self.assertEqual(sketch.RelationManager.added, [])

    def test_relation_not_counted_stops_before_the_dimensions(self) -> None:
        for refused, angle, step in ((9, 15, "centre_on_origin"), (4, 15, "reference_horizontal"),
                                     (5, 90, "radius_vertical")):
            with self.subTest(step=step):
                doc = FakeHoleDoc(FakeHoleSketch(refuses={refused}))
                data = self.assert_stopped(self.call(doc, start_angle_deg=angle), step)
                self.assertEqual(doc.dimensions, [])
                self.assertEqual(data["created"]["dimensions"], [])

    def test_each_dimension_fault_stops_at_that_dimension(self) -> None:
        cases = [("pitch", "no_display", "pitch_dimension", "did not create"),
                 ("hole", "no_dimension", "hole_dimension", "did not hand it back"),
                 ("angle", "wrong_type", "angle_dimension", "of type 2, not 3"),
                 ("pitch", "rejects", "pitch_dimension", "reads 0.001"),
                 ("hole", "no_rename", "hole_dimension", "not renamed to Bohrung"),
                 ("angle", "driven", "angle_dimension", "driven True"),
                 ("pitch", "driven_unreadable", "pitch_dimension", "driven None")]
        for key, fault, step, why in cases:
            with self.subTest(fault=fault, dimension=key):
                doc = FakeHoleDoc(FakeHoleSketch(), {key: fault})
                answer = self.call(doc, start_angle_deg=15)
                data = self.assert_stopped(answer, step)
                self.assertIn(why, answer["message"])
                self.assertEqual(len(data["created"]["dimensions"]), doc.order.index(key))
                self.assertEqual(len(doc.dimensions), doc.order.index(key) + (fault != "no_display"))
                if fault not in ("no_display", "no_dimension"):
                    self.assertTrue(data["failed_dimension"].endswith("@Loecher_Skizze@Part1.Part"), data)

    def test_com_error_mid_call_reports_the_step_and_what_was_created(self) -> None:
        def boom(*args):
            raise RuntimeError("COM error")

        cases = [("create", "geometry", 0), ("relation", "hole_centre_on_pitch_circle", 0),
                 ("diameter", "hole_dimension", 1), ("angle", "angle_dimension", 2)]
        for where, step, dims in cases:
            with self.subTest(where=where):
                sketch = FakeHoleSketch()
                doc = FakeHoleDoc(sketch)
                if where == "create":
                    sketch.CreateLine = boom
                elif where == "relation":
                    real = sketch.RelationManager.AddRelation
                    sketch.RelationManager.AddRelation = (
                        lambda entities, code: boom() if sketch.RelationManager.count == 1 else real(entities, code))
                elif where == "diameter":
                    real_add = doc.AddDiameterDimension2
                    doc.AddDiameterDimension2 = lambda x, y, z: boom() if doc.dimensions else real_add(x, y, z)
                else:
                    doc.AddDimension2 = boom
                answer = self.call(doc, start_angle_deg=15)
                data = self.assert_stopped(answer, step)
                self.assertIn("raised RuntimeError: COM error", answer["message"])
                self.assertEqual(len(data["created"]["dimensions"]), dims)
                self.assertEqual(data["sketch_status"], "fully_defined")
                if where == "create":   # both circles are in the sketch and reported, no segment after the error
                    self.assertEqual(data["created"]["segments"], ["pitch", "hole"])
                    self.assertEqual(sketch.created, ["circle", "circle"])
                if where in ("diameter", "angle"):
                    self.assertEqual(len(data["created"]["relations"]), 7)

    def test_com_error_while_checking_a_created_dimension_names_it(self) -> None:
        def boom(*args):
            raise RuntimeError("COM error")

        doc = FakeHoleDoc(FakeHoleSketch())
        real_add = doc.AddDiameterDimension2

        def add_then_fail(x, y, z):
            display = real_add(x, y, z)
            if len(doc.dimensions) == 2:
                doc.dimensions[1].SetSystemValue3 = boom
            return display

        doc.AddDiameterDimension2 = add_then_fail
        answer = self.call(doc, start_angle_deg=15)
        data = self.assert_stopped(answer, "hole_dimension")
        self.assertIn("raised RuntimeError: COM error while checking the Bohrung dimension", answer["message"])
        self.assertEqual(data["failed_dimension"], "D2@Loecher_Skizze@Part1.Part")
        self.assertEqual(len(data["created"]["dimensions"]), 1)

    def test_unreadable_segments_draw_nothing(self) -> None:
        sketch = FakeHoleSketch()

        def unreadable():
            raise RuntimeError("COM")
        sketch.GetSketchSegments = unreadable
        answer = self.call(FakeHoleDoc(sketch))
        self.assertFalse(answer["ok"], answer)
        self.assertIn("cannot be read", answer["message"])
        self.assertEqual(sketch.created, [])

    def test_unselectable_entity_stops(self) -> None:
        doc = FakeHoleDoc(FakeHoleSketch())
        self.assert_stopped(self.call(doc, selects=False), "pitch_dimension")
        self.assertEqual(doc.dimensions, [])

    def test_extra_or_unreadable_dimension_count_stops(self) -> None:
        for extra in (1, -1, None):
            with self.subTest(extra=extra):
                answer = self.call(FakeHoleDoc(FakeHoleSketch()), extra_dimensions=extra, start_angle_deg=15)
                data = self.assert_stopped(answer, "dimension_count")
                self.assertEqual(len(data["created"]["dimensions"]), 3)

    def test_sketch_not_fully_defined_stops(self) -> None:
        for code, name in ((UNDER, "under_defined"), (1, "unknown"), (OVER, "over_defined"),
                           (RuntimeError("COM"), "unknown")):
            with self.subTest(status=name):
                answer = self.call(FakeHoleDoc(FakeHoleSketch(status=code)))
                data = self.assert_stopped(answer, "sketch_status")
                self.assertIn(name, answer["message"])
                self.assertEqual(data["sketch_status"], name)

    def test_no_sketch_or_a_sketch_with_segments_draws_nothing(self) -> None:
        answer = self.call(FakeHoleDoc(None))
        self.assertFalse(answer["ok"])
        self.assertIn("No sketch is open", answer["message"])
        busy = FakeHoleSketch(segments=[FakeHoleSegment(9, {})])
        answer = self.call(FakeHoleDoc(busy))
        self.assertFalse(answer["ok"])
        self.assertIn("not empty", answer["message"])
        self.assertEqual(busy.created, [])

    def test_bad_input_draws_nothing(self) -> None:
        for args in ({"names": {"pitch": "X", "hole": "X"}}, {"names": {"angle": ""}}, {"start_angle_deg": math.nan},
                     {"hole_diameter_mm": 0}, {"pitch_diameter_mm": -5}, {"names": {"depth": "T"}}):
            with self.subTest(args=args):
                sketch = FakeHoleSketch()
                answer = self.call(FakeHoleDoc(sketch), **args)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(sketch.created, [])


if __name__ == "__main__":
    unittest.main()
