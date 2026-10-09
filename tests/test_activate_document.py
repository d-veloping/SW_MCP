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

"""Offline tests for activate_document (ClauSW issue #115).

The tool activates exactly one open document by its exact title and waits like open_document: only while exactly
the previously active document is still reported as active. It activates nothing when the title is unknown, not
unique or unreadable, and nothing when the document is already active.
"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_file

from test_new_document import Clock, FakeDoc


class Unreadable(FakeDoc):
    def GetTitle(self) -> str:  # noqa: N802 - COM member name
        raise RuntimeError("RPC server unavailable")


class FakeApp:
    """ActiveDoc is `before` until ActivateDoc3, then one entry of `after` per read, the last one repeating."""

    def __init__(self, documents: list[FakeDoc], before: FakeDoc | None, after: list[FakeDoc | None],
                 activates: bool = True) -> None:
        self.documents = documents
        self.before = before
        self.after = after
        self.activates = activates
        self.activations: list[str] = []
        self.reads_after = 0

    def GetDocuments(self):  # noqa: N802 - COM member name
        return self.documents

    @property
    def ActiveDoc(self):  # noqa: N802 - COM member name
        if not self.activations:
            return self.before
        index = min(self.reads_after, len(self.after) - 1)
        self.reads_after += 1
        return self.after[index]

    def ActivateDoc3(self, name, silent, option, errors):  # noqa: N802 - COM member name
        self.activations.append(name)
        return self.activates


PART = FakeDoc("Test_Rahmen", r"C:\Ausgabe\Test_Rahmen.SLDPRT")
DRAWING = FakeDoc("Draw7 - Blatt1")
THIRD = FakeDoc("Fremd", r"C:\Demo\Fremd.SLDPRT")


class ActivateDocumentTests(unittest.TestCase):
    def activate(self, app: FakeApp, title: str = "Draw7 - Blatt1") -> tuple[dict, Clock]:
        clock = Clock()
        with unittest.mock.patch.object(sw_file, "running_app", return_value=app), \
                unittest.mock.patch.object(sw_file.time, "monotonic", clock.monotonic), \
                unittest.mock.patch.object(sw_file.time, "sleep", clock.sleep):
            return sw_file.activate_document({"title": title}), clock

    def test_activates_and_reports_at_once(self) -> None:
        app = FakeApp([PART, DRAWING], PART, [DRAWING])
        answer, clock = self.activate(app)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(app.activations, ["Draw7 - Blatt1"])
        self.assertEqual((answer["data"]["document"]["title"], answer["data"]["activated"]), ("Draw7 - Blatt1", True))
        self.assertEqual(clock.sleeps, 0)

    def test_waits_while_exactly_the_previous_document_is_active(self) -> None:
        app = FakeApp([PART, DRAWING], PART, [PART, PART, DRAWING])
        answer, clock = self.activate(app)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((clock.sleeps, len(app.activations)), (2, 1))   # never activated a second time
        self.assertGreater(answer["data"]["wait_ms"], 0)

    def test_third_document_ends_the_wait(self) -> None:
        app = FakeApp([PART, DRAWING, THIRD], PART, [PART, THIRD, DRAWING])
        answer, _ = self.activate(app)
        self.assertFalse(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual((data["document"]["title"], data["activated"]), ("Draw7 - Blatt1", False))
        self.assertEqual(data["active_document"]["title"], "Fremd")
        self.assertEqual(app.reads_after, 2)

    def test_deadline_ends_the_wait(self) -> None:
        answer, _ = self.activate(FakeApp([PART, DRAWING], PART, [PART]))
        self.assertFalse(answer["ok"], answer)
        self.assertGreaterEqual(answer["data"]["wait_ms"], sw_file.NEW_DOCUMENT_ACTIVATION_S * 1000)
        self.assertEqual(answer["data"]["active_document"]["title"], "Test_Rahmen")

    def test_unknown_duplicate_or_unreadable_title_activates_nothing(self) -> None:
        faelle = {
            "unbekannt": [PART, THIRD],
            "doppelt": [PART, DRAWING, FakeDoc("Draw7 - Blatt1", r"C:\x\Draw7.SLDDRW")],
            "unlesbar": [PART, Unreadable("?"), DRAWING],
        }
        for name, documents in faelle.items():
            with self.subTest(name=name):
                app = FakeApp(documents, PART, [DRAWING])
                answer, _ = self.activate(app)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(app.activations, [])

    def test_prefix_or_stem_is_not_a_match(self) -> None:
        for title in ("Draw7", "draw7 - blatt1", ""):
            with self.subTest(title=title):
                app = FakeApp([PART, DRAWING], PART, [DRAWING])
                self.assertFalse(self.activate(app, title)[0]["ok"])
                self.assertEqual(app.activations, [])

    def test_already_active_does_not_activate(self) -> None:
        app = FakeApp([PART, DRAWING], DRAWING, [DRAWING])
        answer, _ = self.activate(app)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((app.activations, answer["data"]["wait_ms"]), ([], 0))

    def test_refused_activation_is_a_failure_with_the_document(self) -> None:
        app = FakeApp([PART, DRAWING], PART, [PART], activates=False)
        answer, _ = self.activate(app)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["data"]["document"]["title"], "Draw7 - Blatt1")
        self.assertEqual(answer["data"]["active_document"]["title"], "Test_Rahmen")


if __name__ == "__main__":
    unittest.main()
