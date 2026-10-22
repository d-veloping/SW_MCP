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

"""Offline tests for the inspection tools: check_errors reports unsolved sketches only on request (#14)."""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_inspect  # noqa: E402


class CheckErrorsSketchTests(unittest.TestCase):
    PROBLEM = {"feature": "Boss-Extrude1", "code": 1, "is_warning": False}

    def call(self, args: dict, problems: list, unsolved: list) -> tuple[dict, unittest.mock.Mock]:
        walk = unittest.mock.Mock(return_value=unsolved)
        with unittest.mock.patch.object(sw_inspect, "active_document", return_value=(None, object())), \
                unittest.mock.patch.object(sw_inspect, "rebuild", return_value=True), \
                unittest.mock.patch.object(sw_inspect, "whats_wrong", return_value=problems), \
                unittest.mock.patch.object(sw_inspect, "unsolved_sketches", walk):
            return sw_inspect.check_errors(args), walk

    def test_default_does_not_walk_the_sketches_and_answers_as_before(self) -> None:
        answer, walk = self.call({}, [], [{"sketch": "P_Skizze", "sketch_status": "no_solution"}])
        walk.assert_not_called()
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["message"], "No feature errors or warnings.")
        self.assertEqual(answer["data"], {"problems": [], "rebuilt": True, "sketches_checked": False})

        answer, walk = self.call({"sketches": False}, [self.PROBLEM], [])
        walk.assert_not_called()
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["problems"], [self.PROBLEM])
        self.assertNotIn("unsolved_sketches", answer["data"])

    def test_unsolved_sketch_fails_the_check_without_feature_problems(self) -> None:
        unsolved = [{"sketch": "P_Skizze", "sketch_status": "no_solution"}]
        answer, walk = self.call({"sketches": True}, [], unsolved)
        walk.assert_called_once()
        self.assertFalse(answer["ok"])
        self.assertIn("P_Skizze: no_solution", answer["message"])
        self.assertEqual(answer["data"]["problems"], [])
        self.assertEqual(answer["data"]["unsolved_sketches"], unsolved)
        self.assertTrue(answer["data"]["sketches_checked"])

    def test_every_sketch_solved_keeps_ok(self) -> None:
        answer, _ = self.call({"sketches": True}, [], [])
        self.assertTrue(answer["ok"])
        self.assertIn("every sketch solves", answer["message"])
        self.assertEqual(answer["data"]["unsolved_sketches"], [])

    def test_feature_problems_and_unsolved_sketches_are_both_named(self) -> None:
        answer, _ = self.call({"sketches": True}, [self.PROBLEM], [{"sketch": "Skizze2", "sketch_status": "over_defined"}])
        self.assertFalse(answer["ok"])
        self.assertIn("1 features are flagged", answer["message"])
        self.assertIn("Skizze2: over_defined", answer["message"])


class ExactBoundingBoxTests(unittest.TestCase):
    """`exact` spans every solid body, hidden ones included: a hidden body outside the visible one widens the box."""

    class Body:
        def __init__(self, box: list[float]) -> None:
            self.box = box

    class Part:
        def __init__(self, visible: list, hidden: list) -> None:
            self.visible, self.hidden, self.calls = visible, hidden, []

        def GetBodies2(self, kind: int, visible_only: bool) -> tuple:
            self.calls.append((kind, visible_only))
            return tuple(self.visible if visible_only else self.visible + self.hidden)

    @staticmethod
    def union(bodies: list) -> list[float]:
        return [min(b.box[i] for b in bodies) for i in range(3)] + [max(b.box[i + 3] for b in bodies) for i in range(3)]

    def call(self, part: "ExactBoundingBoxTests.Part") -> dict:
        with unittest.mock.patch.object(sw_inspect, "active_document", return_value=(None, part)), \
                unittest.mock.patch.object(sw_inspect, "document_type", return_value=1), \
                unittest.mock.patch.object(sw_inspect, "bodies_extents", side_effect=self.union):
            return sw_inspect.get_bounding_box({"exact": True})

    def test_a_hidden_body_widens_the_exact_box(self) -> None:
        part = self.Part([self.Body([0, 0, 0, 0.1, 0.05, 0.01])], [self.Body([0.2, 0, 0, 0.3, 0.05, 0.01])])
        answer = self.call(part)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(part.calls, [(0, False)])
        self.assertEqual((answer["data"]["min_mm"], answer["data"]["max_mm"]), ([0, 0, 0], [300, 50, 10]))
        self.assertTrue(answer["data"]["exact"])

    def test_no_solid_body_is_an_error(self) -> None:
        answer = self.call(self.Part([], []))
        self.assertFalse(answer["ok"])
        self.assertIn("no solid body", answer["message"])


if __name__ == "__main__":
    unittest.main()
