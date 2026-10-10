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
    UNSOLVED = {"sketch": "P_Skizze", "sketch_status": "no_solution"}
    SOLVED = {"sketch": "K_Skizze", "sketch_status": "fully_defined"}
    UNREADABLE = {"sketch": "Skizze2", "sketch_status": "unknown"}

    def call(self, args: dict, problems: list, states: list) -> tuple[dict, unittest.mock.Mock]:
        walk = unittest.mock.Mock(return_value=states)
        with unittest.mock.patch.object(sw_inspect, "active_document", return_value=(None, object())), \
                unittest.mock.patch.object(sw_inspect, "rebuild", return_value=True), \
                unittest.mock.patch.object(sw_inspect, "whats_wrong", return_value=problems), \
                unittest.mock.patch.object(sw_inspect, "sketch_states", walk):
            return sw_inspect.check_errors(args), walk

    def test_default_does_not_walk_the_sketches_and_answers_as_before(self) -> None:
        answer, walk = self.call({}, [], [self.UNSOLVED])
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
        answer, walk = self.call({"sketches": True}, [], [self.SOLVED, self.UNSOLVED])
        walk.assert_called_once()
        self.assertFalse(answer["ok"])
        self.assertIn("P_Skizze: no_solution", answer["message"])
        self.assertEqual(answer["data"]["problems"], [])
        self.assertEqual(answer["data"]["unsolved_sketches"], [self.UNSOLVED])
        self.assertEqual(answer["data"]["unreadable_sketches"], [])
        self.assertTrue(answer["data"]["sketches_checked"])

    def test_every_sketch_solved_keeps_ok(self) -> None:
        answer, _ = self.call({"sketches": True}, [], [self.SOLVED])
        self.assertTrue(answer["ok"])
        self.assertIn("no sketch reads an unsolved state", answer["message"])
        self.assertEqual(answer["data"]["unsolved_sketches"], [])

    def test_unreadable_sketch_is_named_and_does_not_fail_the_check(self) -> None:
        answer, _ = self.call({"sketches": True}, [], [self.SOLVED, self.UNREADABLE])
        self.assertTrue(answer["ok"])
        self.assertIn("1 sketches could not be read (Skizze2)", answer["message"])
        self.assertNotIn("every sketch", answer["message"])
        self.assertEqual(answer["data"]["unreadable_sketches"], [self.UNREADABLE])
        self.assertEqual(answer["data"]["unsolved_sketches"], [])

    def test_feature_problems_and_unsolved_sketches_are_both_named(self) -> None:
        answer, _ = self.call({"sketches": True}, [self.PROBLEM],
                              [{"sketch": "Skizze2", "sketch_status": "over_defined"}, self.UNREADABLE])
        self.assertFalse(answer["ok"])
        self.assertIn("1 features are flagged", answer["message"])
        self.assertIn("Skizze2: over_defined", answer["message"])
        self.assertIn("could not be read", answer["message"])


if __name__ == "__main__":
    unittest.main()
