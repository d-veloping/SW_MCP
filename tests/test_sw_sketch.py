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
