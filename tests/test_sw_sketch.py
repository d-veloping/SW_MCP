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


if __name__ == "__main__":
    unittest.main()
