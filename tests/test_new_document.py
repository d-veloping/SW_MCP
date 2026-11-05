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

"""Offline tests for create_new_document and the activation of the new document.

SOLIDWORKS 2016 can return from NewDocument while it still reports the previous
document as active.  create_new_document waits for the new one, but only as
long as exactly the previous document is active; any other document ends the
wait at once, and the tool never activates anything itself, so it cannot undo
a switch someone made by hand.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_file


class FakeDoc:
    def __init__(self, title: str, path: str = "") -> None:
        self.title = title
        self.path = path

    def GetTitle(self) -> str:  # noqa: N802 - COM member name
        return self.title

    def GetPathName(self) -> str:  # noqa: N802 - COM member name
        return self.path

    def GetType(self) -> int:  # noqa: N802 - COM member name
        return 1

    def GetSaveFlag(self) -> bool:  # noqa: N802 - COM member name
        return False


class FakeApp:
    """ActiveDoc follows a script: `before` until NewDocument, then one entry per read, the last one repeating."""

    def __init__(self, before: FakeDoc | None, after: list[FakeDoc | None], new: FakeDoc | None) -> None:
        self.before = before
        self.after = after
        self.new = new
        self.created = False
        self.reads_after = 0
        self.preference = __file__   # any existing file stands in for the configured template
        self.preference_reads = 0
        self.templates: list[str] = []

    def GetUserPreferenceStringValue(self, preference: int) -> str:  # noqa: N802 - COM member name
        self.preference_reads += 1
        return self.preference

    def NewDocument(self, template, size, width, height):  # noqa: N802 - COM member name
        self.created = True
        self.templates.append(template)
        return self.new

    @property
    def ActiveDoc(self):  # noqa: N802 - COM member name
        if not self.created:
            return self.before
        index = min(self.reads_after, len(self.after) - 1)
        self.reads_after += 1
        return self.after[index]

    def ActivateDoc3(self, *args):  # noqa: N802 - COM member name
        raise AssertionError("create_new_document must never activate a document itself")


class Clock:
    """time.monotonic and time.sleep for sw_file: sleeping advances the clock."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


PREVIOUS = FakeDoc("Part1")
NEW = FakeDoc("Part2")
THIRD = FakeDoc("Fremd", r"C:\Demo\Fremd.SLDPRT")


class NewDocumentActivationTests(unittest.TestCase):
    def create(self, app: FakeApp) -> tuple[dict, Clock]:
        clock = Clock()
        with unittest.mock.patch.object(sw_file, "running_app", return_value=app), \
                unittest.mock.patch.object(sw_file.time, "monotonic", clock.monotonic), \
                unittest.mock.patch.object(sw_file.time, "sleep", clock.sleep):
            return sw_file.create_new_document({"kind": "part"}), clock

    def test_new_document_active_at_once(self) -> None:
        answer, clock = self.create(FakeApp(PREVIOUS, [NEW], NEW))
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["data"]["document"]["title"], "Part2")
        self.assertTrue(answer["data"]["activated"])
        self.assertEqual(answer["data"]["wait_ms"], 0)
        self.assertEqual(clock.sleeps, 0)
        self.assertEqual(answer["data"]["template"], __file__)
        self.assertEqual(answer["data"]["template_source"], "preference")

    def test_waits_while_exactly_the_previous_document_is_active(self) -> None:
        answer, clock = self.create(FakeApp(PREVIOUS, [PREVIOUS, PREVIOUS, FakeDoc("Part1"), NEW], NEW))
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["data"]["activated"])
        self.assertEqual(clock.sleeps, 3)
        self.assertGreater(answer["data"]["wait_ms"], 0)

    def test_waits_while_no_document_was_and_is_active(self) -> None:
        answer, _ = self.create(FakeApp(None, [None, NEW], NEW))
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["data"]["activated"])

    def test_third_document_ends_the_wait_at_once(self) -> None:
        for after in ([THIRD], [PREVIOUS, THIRD, NEW], [FakeDoc("Part1", r"C:\Demo\Part1.SLDPRT")], [None]):
            with self.subTest(after=[d.title if d else None for d in after]):
                app = FakeApp(PREVIOUS, after, NEW)
                answer, clock = self.create(app)
                self.assertFalse(answer["ok"], answer)
                data = answer["data"]
                self.assertEqual(data["document"]["title"], "Part2")
                self.assertFalse(data["activated"])
                self.assertIn("wait_ms", data)
                self.assertEqual((data["template"], data["template_source"]), (__file__, "preference"))
                third = next(d for d in after if d is not PREVIOUS)
                self.assertEqual(data["active_document"], None if third is None else sw_file.document_info(third))
                self.assertEqual(app.reads_after, after.index(third) + 1)   # no read after the third one

    def test_deadline_ends_the_wait_with_ok_false(self) -> None:
        answer, clock = self.create(FakeApp(PREVIOUS, [PREVIOUS], NEW))
        self.assertFalse(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual(data["document"]["title"], "Part2")
        self.assertFalse(data["activated"])
        self.assertGreaterEqual(data["wait_ms"], sw_file.NEW_DOCUMENT_ACTIVATION_S * 1000)
        self.assertLessEqual(data["wait_ms"], (sw_file.NEW_DOCUMENT_ACTIVATION_S + sw_file.NEW_DOCUMENT_POLL_S) * 1000)
        self.assertEqual(data["active_document"]["title"], "Part1")
        self.assertIn("previous", answer["message"])
        self.assertEqual((data["template"], data["template_source"]), (__file__, "preference"))

    def test_no_document_from_new_document_is_a_plain_failure(self) -> None:
        answer, _ = self.create(FakeApp(PREVIOUS, [PREVIOUS], None))
        self.assertFalse(answer["ok"])
        self.assertNotIn("data", answer)


class TemplatePathTests(unittest.TestCase):
    """template_path names exactly the template; a value that cannot be used creates no document and never falls
    back to the SOLIDWORKS default."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.part = self.folder / "Standard Part.prtdot"
        self.drawing = self.folder / "Standard Drawing.DRWDOT"
        for template in (self.part, self.drawing):
            template.write_bytes(b"")

    def create(self, app: FakeApp, kind: str = "part", **extra) -> dict:
        with unittest.mock.patch.object(sw_file, "running_app", return_value=app),                 unittest.mock.patch.object(sw_file, "discover_template", side_effect=AssertionError("no search")):
            return sw_file.create_new_document({"kind": kind, **extra})

    def test_named_template_is_handed_to_solidworks_and_reported(self) -> None:
        app = FakeApp(PREVIOUS, [NEW], NEW)
        answer = self.create(app, template_path=str(self.part))
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(app.templates, [str(self.part)])
        self.assertEqual(app.preference_reads, 0)
        self.assertEqual(answer["data"]["template"], str(self.part))
        self.assertEqual(answer["data"]["template_source"], "argument")

    def test_extension_is_matched_without_case_and_per_kind(self) -> None:
        app = FakeApp(PREVIOUS, [NEW], NEW)
        answer = self.create(app, "drawing", template_path=str(self.drawing))
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(app.templates, [str(self.drawing)])

    def test_named_template_is_reported_on_a_failed_activation(self) -> None:
        clock = Clock()
        with unittest.mock.patch.object(sw_file.time, "monotonic", clock.monotonic),                 unittest.mock.patch.object(sw_file.time, "sleep", clock.sleep):
            answer = self.create(FakeApp(PREVIOUS, [THIRD], NEW), template_path=str(self.part))
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["data"]["document"]["title"], "Part2")
        self.assertEqual((answer["data"]["template"], answer["data"]["template_source"]), (str(self.part), "argument"))

    def test_unusable_template_path_creates_nothing(self) -> None:
        cases = {
            "relative": "Standard Part.prtdot",
            "drive relative": "C:Standard Part.prtdot",
            "missing file": str(self.folder / "Fehlt.prtdot"),
            "folder": str(self.folder / "Ordner.prtdot"),
            "assembly template for a part": str(self.part.with_suffix(".asmdot")),
            "drawing template for a part": str(self.drawing),
            "empty": "",
            "blank": "   ",
            "not a string": 12,
        }
        (self.folder / "Ordner.prtdot").mkdir()
        self.part.with_suffix(".asmdot").write_bytes(b"")
        for name, template_path in cases.items():
            with self.subTest(name):
                app = FakeApp(PREVIOUS, [NEW], NEW)
                answer = self.create(app, template_path=template_path)
                self.assertFalse(answer["ok"], answer)
                self.assertIn("template_path", answer["message"])
                self.assertNotIn("data", answer)
                self.assertFalse(app.created)
                self.assertEqual(app.preference_reads, 0)

    def test_part_template_for_a_drawing_creates_nothing(self) -> None:
        app = FakeApp(PREVIOUS, [NEW], NEW)
        answer = self.create(app, "drawing", template_path=str(self.part))
        self.assertFalse(answer["ok"], answer)
        self.assertIn(".drwdot", answer["message"])
        self.assertFalse(app.created)

    def test_without_template_path_a_discovered_template_is_reported_as_such(self) -> None:
        app = FakeApp(PREVIOUS, [NEW], NEW)
        app.preference = ""
        with unittest.mock.patch.object(sw_file, "running_app", return_value=app),                 unittest.mock.patch.object(sw_file, "discover_template", return_value=self.part):
            answer = sw_file.create_new_document({"kind": "part"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(app.templates, [str(self.part)])
        self.assertEqual((answer["data"]["template"], answer["data"]["template_source"]), (str(self.part), "discovered"))


if __name__ == "__main__":
    unittest.main()
