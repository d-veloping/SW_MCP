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

"""Offline tests for thin features on boss_extrude, cut_extrude and revolve (#37).

A fake FeatureManager records which member each handler calls and with which arguments. Without thin_mm the calls
must stay exactly what they were before thin features existed; with thin_mm the thin member gets the wall type and
thicknesses at the positions the SOLIDWORKS 2016 SP3 type library declares (FeatureExtrusionThin2 30 arguments,
FeatureCutThin2 29, FeatureCutThin 26, FeatureRevolve2 20). Whether SOLIDWORKS builds the intended body is the live
check's job (tests/live_thin_features.py).
"""

from __future__ import annotations

import math
import sys
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_core, sw_feature  # noqa: E402


class FakeFeatureManager:
    """Members resolve like pywin32's dynamic dispatch: an unknown name raises AttributeError."""

    def __init__(self, known: set[str], returns: Any = "feature") -> None:
        self.known = set(known)
        self.returns = returns
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in self.known:
            raise AttributeError(name)

        def method(*args):
            self.calls.append((name, args))
            return self.returns(name, len(self.calls)) if callable(self.returns) else self.returns

        return method


ALL_MEMBERS = {"FeatureExtrusion3", "FeatureExtrusionThin2", "FeatureCut4", "FeatureCut3", "FeatureCutThin2",
               "FeatureCutThin", "FeatureRevolve2"}
SW2016_MEMBERS = ALL_MEMBERS - {"FeatureCut4"}

# The argument lists the handlers sent before #37, with depth_mm 10 and angle_deg 360 as defaults.
SOLID_EXTRUSION = (
    True, False, False, 0, 0, 0.01, 0.01,
    False, False, False, False, 0.0, 0.0,
    False, False, False, False,
    True, True, True,
    0, 0.0, False,
)
SOLID_CUT4 = (
    True, False, False, 0, 0, 0.01, 0.01,
    False, False, False, False, 0.0, 0.0,
    False, False, False, False,
    False, True, True,
    False, False, False,
    0, 0.0, False, False,
)
SOLID_REVOLVE = (
    True, True, False, False, False, False,
    0, 0, 2 * math.pi, 0.0,
    False, False, 0.0, 0.0,
    0, 0.0, 0.0,
    True, True, True,
)


class HandlerHarness(unittest.TestCase):
    """Runs the real handlers and the real feature_result envelope against a fake document."""

    def setUp(self) -> None:
        self._unflaggable = set(sw_core._UNFLAGGABLE_NAMES)
        sw_core._UNFLAGGABLE_NAMES.clear()
        self.manager = FakeFeatureManager(ALL_MEMBERS)
        self.require_part = unittest.mock.Mock(return_value=(None, object()))
        patches = [
            unittest.mock.patch.object(sw_feature, "require_part", self.require_part),
            unittest.mock.patch.object(sw_feature, "select_sketch_for_feature", return_value="Sketch1"),
            unittest.mock.patch.object(sw_feature, "feature_manager", side_effect=lambda doc: self.manager),
            unittest.mock.patch.object(sw_feature, "require_selection", return_value=1),
            unittest.mock.patch.object(sw_feature, "rename_feature", return_value=None),
            unittest.mock.patch.object(sw_core, "feature_property", return_value="Feature1"),
            unittest.mock.patch.object(sw_core, "rebuild", return_value=True),
            unittest.mock.patch.object(sw_core, "whats_wrong", return_value=[]),
            unittest.mock.patch.object(sw_core, "clear_selection", return_value=None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        sw_core._UNFLAGGABLE_NAMES.clear()
        sw_core._UNFLAGGABLE_NAMES.update(self._unflaggable)


class SolidCallsUnchangedTests(HandlerHarness):
    """Acceptance 3: without thin_mm every handler sends exactly the call it sent before."""

    def test_boss_extrude_without_thin(self) -> None:
        answer = sw_feature.boss_extrude({})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.manager.calls, [("FeatureExtrusion3", SOLID_EXTRUSION)])
        self.assertNotIn("thin_mm", answer["data"])

    def test_cut_extrude_without_thin(self) -> None:
        answer = sw_feature.cut_extrude({})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.manager.calls, [("FeatureCut4", SOLID_CUT4)])

    def test_cut_extrude_without_thin_on_2016(self) -> None:
        self.manager.known = set(SW2016_MEMBERS)
        sw_feature.cut_extrude({})
        self.assertEqual(self.manager.calls, [("FeatureCut3", SOLID_CUT4[:26])])

    def test_revolve_without_thin(self) -> None:
        answer = sw_feature.revolve({})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.manager.calls, [("FeatureRevolve2", SOLID_REVOLVE)])


class ThinCallTests(HandlerHarness):
    def test_boss_extrude_thin_sides(self) -> None:
        for side, wall_type in sw_core.THIN_SIDES.items():
            with self.subTest(side=side):
                self.manager.calls.clear()
                args: dict[str, Any] = {"depth_mm": 20, "thin_mm": 5, "thin_side": side}
                if side == "two_side":
                    args["thin_mm_2"] = 2
                answer = sw_feature.boss_extrude(args)
                self.assertTrue(answer["ok"], answer)
                [(name, call)] = self.manager.calls
                self.assertEqual(name, "FeatureExtrusionThin2")
                self.assertEqual(len(call), 30)
                self.assertEqual(call[5], 0.02)              # D1
                self.assertTrue(call[17])                    # Merge
                self.assertAlmostEqual(call[18], 0.005)      # Thk1
                self.assertAlmostEqual(call[19], 0.002 if side == "two_side" else 0.0)  # Thk2
                self.assertEqual(call[21], wall_type)        # RevThinDir
                self.assertEqual(answer["data"]["thin_mm"], 5)
                self.assertEqual(answer["data"]["thin_side"], side)
                self.assertIn("thin boss extrusion", answer["message"])

    def test_boss_extrude_thin_defaults_to_one_side(self) -> None:
        answer = sw_feature.boss_extrude({"thin_mm": 3})
        [(_, call)] = self.manager.calls
        self.assertEqual(call[21], sw_core.THIN_SIDES["one_side"])
        self.assertEqual(answer["data"]["thin_side"], "one_side")

    def test_boss_extrude_thin_keeps_merge_and_end_condition(self) -> None:
        sw_feature.boss_extrude({"thin_mm": 3, "merge": False, "end_condition": "through_all_both"})
        [(_, call)] = self.manager.calls
        self.assertEqual(call[:5], (False, False, False, 1, 1))
        self.assertFalse(call[17])

    def test_cut_extrude_thin(self) -> None:
        answer = sw_feature.cut_extrude({"depth_mm": 5, "thin_mm": 2, "thin_side": "mid_plane", "flip_side": True})
        self.assertTrue(answer["ok"], answer)
        [(name, call)] = self.manager.calls
        self.assertEqual(name, "FeatureCutThin2")
        self.assertEqual(len(call), 29)
        self.assertTrue(call[1])                         # Flip
        self.assertAlmostEqual(call[17], 0.002)          # Thk1
        self.assertEqual(call[20], sw_core.THIN_SIDES["mid_plane"])
        self.assertIn("thin cut", answer["message"])

    def test_cut_extrude_thin_falls_back_to_featurecutthin(self) -> None:
        self.manager.known = {"FeatureCutThin"}
        answer = sw_feature.cut_extrude({"thin_mm": 2})
        self.assertTrue(answer["ok"], answer)
        [(name, call)] = self.manager.calls
        self.assertEqual(name, "FeatureCutThin")
        self.assertEqual(len(call), 26)
        self.assertAlmostEqual(call[17], 0.002)

    def test_cut_extrude_thin_tries_the_other_direction(self) -> None:
        self.manager.returns = lambda name, n: None if n == 1 else "feature"
        answer = sw_feature.cut_extrude({"thin_mm": 2})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([c[0] for c in self.manager.calls], ["FeatureCutThin2", "FeatureCutThin2"])
        self.assertFalse(self.manager.calls[0][1][2])
        self.assertTrue(self.manager.calls[1][1][2])
        self.assertTrue(answer["data"]["reversed_automatically"])

    def test_revolve_thin(self) -> None:
        answer = sw_feature.revolve({"thin_mm": 3, "thin_side": "mid_plane"})
        self.assertTrue(answer["ok"], answer)
        [(name, call)] = self.manager.calls
        self.assertEqual(name, "FeatureRevolve2")
        self.assertEqual(len(call), 20)
        self.assertTrue(call[1])                         # IsSolid
        self.assertTrue(call[2])                         # IsThin
        self.assertEqual(call[14], sw_core.THIN_SIDES["mid_plane"])
        self.assertAlmostEqual(call[15], 0.003)
        self.assertEqual(call[16], 0.0)
        self.assertIn("thin revolve", answer["message"])

    def test_revolve_thin_cut_two_side(self) -> None:
        sw_feature.revolve({"thin_mm": 3, "thin_side": "two_side", "thin_mm_2": 1, "cut": True})
        [(_, call)] = self.manager.calls
        self.assertTrue(call[3])                         # IsCut
        self.assertEqual(call[14], sw_core.THIN_SIDES["two_side"])
        self.assertAlmostEqual(call[16], 0.001)


class ThinFailureTests(HandlerHarness):
    """Acceptance 4: the thin call alone fails and the handler answers ok false."""

    def test_each_handler_reports_a_failed_thin_call(self) -> None:
        self.manager.returns = None
        cases = [
            (sw_feature.boss_extrude, {"thin_mm": 3}, ["FeatureExtrusionThin2"], "thin boss extrusion"),
            (sw_feature.cut_extrude, {"thin_mm": 3}, ["FeatureCutThin2", "FeatureCutThin2"], "thin cut"),
            (sw_feature.revolve, {"thin_mm": 3}, ["FeatureRevolve2"], "thin revolve"),
        ]
        for handler, args, members, action in cases:
            with self.subTest(handler=handler.__name__):
                self.manager.calls.clear()
                answer = handler(args)
                self.assertFalse(answer["ok"])
                self.assertIn(f"did not create the {action}", answer["message"])
                self.assertEqual([c[0] for c in self.manager.calls], members)
                self.assertEqual(answer["data"]["thin_mm"], 3)

    def test_failed_thin_cut_says_neither_direction_removed_material(self) -> None:
        self.manager.returns = None
        answer = sw_feature.cut_extrude({"thin_mm": 3})
        self.assertIn("Neither direction removed material", answer["message"])


class ThinArgumentTests(HandlerHarness):
    """Acceptance 5: invalid thin arguments are refused before SOLIDWORKS is touched."""

    INVALID = [
        {"thin_mm": 0},
        {"thin_mm": -1},
        {"thin_mm": float("nan")},
        {"thin_mm": True},
        {"thin_mm": "3"},
        {"thin_side": "mid_plane"},
        {"thin_mm_2": 2},
        {"thin_mm": 3, "thin_side": "two_side"},
        {"thin_mm": 3, "thin_mm_2": 2},
        {"thin_mm": 3, "thin_side": "one_side", "thin_mm_2": 2},
        {"thin_mm": 3, "thin_side": "two_side", "thin_mm_2": 0},
        {"thin_mm": 3, "thin_side": "outside"},
    ]

    def test_invalid_combinations_are_refused_by_every_handler(self) -> None:
        for handler in (sw_feature.boss_extrude, sw_feature.cut_extrude, sw_feature.revolve):
            for args in self.INVALID:
                with self.subTest(handler=handler.__name__, args=args):
                    answer = handler(dict(args))
                    self.assertFalse(answer["ok"], answer)
                    self.assertEqual(self.manager.calls, [])
                    self.require_part.assert_not_called()

    def test_draft_on_a_thin_extrusion_is_refused(self) -> None:
        for handler in (sw_feature.boss_extrude, sw_feature.cut_extrude):
            with self.subTest(handler=handler.__name__):
                answer = handler({"thin_mm": 3, "draft_deg": 2})
                self.assertFalse(answer["ok"])
                self.assertIn("draft", answer["message"])
                self.assertEqual(self.manager.calls, [])

    def test_schema_offers_the_thin_arguments_on_all_three_tools(self) -> None:
        tools = {t.name: t for t in sw_core.TOOLS}
        for name in ("boss_extrude", "cut_extrude", "revolve"):
            with self.subTest(tool=name):
                properties = tools[name].inputSchema["properties"]
                self.assertEqual(properties["thin_side"]["enum"], sorted(sw_core.THIN_SIDES))
                self.assertIn("thin_mm_2", properties)
                self.assertIn("thin feature", tools[name].description)


class FlagListTests(unittest.TestCase):
    def test_thin_members_are_flagged_as_methods(self) -> None:
        self.assertLessEqual({"FeatureExtrusionThin2", "FeatureCutThin2", "FeatureCutThin"},
                             set(sw_core._FEATURE_MANAGER_METHODS))


if __name__ == "__main__":
    unittest.main()
