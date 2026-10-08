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

"""Offline tests for open_document (opened document, already_open, activation wait), close_document with
only_if_clean, and the unreadable save flag in document_info.

SOLIDWORKS 2016 was seen to report the previously active document right after OpenDoc6 and ActivateDoc3 (ClauSW
issue #107); the answer then named that other document.  A caller that closes what it opened also needs to know,
from the same call, whether the file was open before.
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

from solidworks_mcp import sw_core, sw_file


class FakeDoc:
    def __init__(self, title: str, path: str = "", dirty: object = False) -> None:
        self.title = title
        self.path = path
        self.dirty = dirty

    def GetTitle(self) -> str:  # noqa: N802 - COM member name
        return self.title

    def GetPathName(self) -> str:  # noqa: N802 - COM member name
        return self.path

    def GetType(self) -> int:  # noqa: N802 - COM member name
        return 1

    def GetSaveFlag(self) -> object:  # noqa: N802 - COM member name
        if isinstance(self.dirty, Exception):
            raise self.dirty
        return self.dirty


class Unreadable(FakeDoc):
    def GetPathName(self) -> str:  # noqa: N802 - COM member name
        raise RuntimeError("COM")


class FakeApp:
    """`docs` are the open documents; OpenDoc6 adds `opened` (unless it is open already). ActiveDoc follows `after`
    once ActivateDoc3 ran, one entry per read, the last one repeating."""

    def __init__(self, docs: list[FakeDoc], opened: FakeDoc, active: FakeDoc | None, after: list[FakeDoc | None],
                 activates: bool = True) -> None:
        self.docs = docs
        self.opened = opened
        self.active = active
        self.after = after
        self.activates = activates
        self.activated = False
        self.reads_after = 0
        self.closed: list[str] = []

    def GetDocuments(self):  # noqa: N802 - COM member name
        return tuple(self.docs)

    def GetDocumentCount(self) -> int:  # noqa: N802 - COM member name
        return len(self.docs)

    def OpenDoc6(self, path, doc_type, options, config, errors, warnings):  # noqa: N802 - COM member name
        if self.opened not in self.docs:
            self.docs.append(self.opened)
        return self.opened

    def ActivateDoc3(self, *args):  # noqa: N802 - COM member name
        self.activated = True
        return self.activates

    @property
    def ActiveDoc(self):  # noqa: N802 - COM member name
        if not self.activated:
            return self.active
        index = min(self.reads_after, len(self.after) - 1)
        self.reads_after += 1
        return self.after[index]

    def CloseDoc(self, title):  # noqa: N802 - COM member name
        self.closed.append(title)
        self.docs = [d for d in self.docs if d.title != title]


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


class OpenDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.file = Path(self._tmp.name) / "Bolzen.SLDPRT"
        self.file.write_bytes(b"SW")
        self.part = FakeDoc("Bolzen", str(self.file))
        self.foreign = FakeDoc("Fremd", str(Path(self._tmp.name) / "Fremd.SLDPRT"))

    def open(self, app: FakeApp) -> tuple[dict, Clock]:
        clock = Clock()
        with unittest.mock.patch.object(sw_file, "running_app", return_value=app), \
                unittest.mock.patch.object(sw_file, "byref_long", return_value=unittest.mock.Mock(value=0)), \
                unittest.mock.patch.object(sw_file.time, "monotonic", clock.monotonic), \
                unittest.mock.patch.object(sw_file.time, "sleep", clock.sleep):
            return sw_file.open_document({"path": str(self.file)}), clock

    def test_answer_names_the_opened_document_not_the_previous_active_one(self) -> None:
        """The case from ClauSW #107: right after activation SOLIDWORKS still reports the previous document."""
        app = FakeApp([self.foreign], self.part, self.foreign, [self.foreign, self.foreign, self.part])
        answer, clock = self.open(app)
        self.assertTrue(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual(data["document"]["title"], "Bolzen")
        self.assertEqual(data["document"]["path"], str(self.file))
        self.assertIs(data["already_open"], False)
        self.assertTrue(data["activated"])
        self.assertEqual(clock.sleeps, 2)
        self.assertGreater(data["wait_ms"], 0)

    def test_active_at_once(self) -> None:
        answer, clock = self.open(FakeApp([], self.part, None, [self.part]))
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["data"]["wait_ms"], clock.sleeps), (0, 0))

    def test_already_open_is_read_before_opendoc6(self) -> None:
        same_file_other_case = FakeDoc("Bolzen", str(self.file).upper())
        answer, _ = self.open(FakeApp([self.foreign, same_file_other_case], self.part, self.foreign, [self.part]))
        self.assertTrue(answer["ok"], answer)
        self.assertIs(answer["data"]["already_open"], True)

    def test_already_open_unknown_when_an_entry_cannot_be_read(self) -> None:
        answer, _ = self.open(FakeApp([Unreadable("?")], self.part, None, [self.part]))
        self.assertTrue(answer["ok"], answer)
        self.assertIsNone(answer["data"]["already_open"])

    def test_third_document_ends_the_wait_with_ok_false(self) -> None:
        third = FakeDoc("Dritt", r"C:\Demo\Dritt.SLDPRT")
        app = FakeApp([self.foreign], self.part, self.foreign, [self.foreign, third, self.part])
        answer, _ = self.open(app)
        self.assertFalse(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual(data["document"]["title"], "Bolzen")
        self.assertFalse(data["activated"])
        self.assertEqual(data["active_document"]["title"], "Dritt")
        self.assertEqual(app.reads_after, 2)

    def test_deadline_ends_the_wait_with_ok_false(self) -> None:
        answer, _ = self.open(FakeApp([self.foreign], self.part, self.foreign, [self.foreign]))
        self.assertFalse(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual(data["document"]["title"], "Bolzen")
        self.assertEqual(data["active_document"]["title"], "Fremd")
        self.assertGreaterEqual(data["wait_ms"], sw_file.NEW_DOCUMENT_ACTIVATION_S * 1000)

    def test_failed_activation_keeps_document_and_already_open(self) -> None:
        answer, _ = self.open(FakeApp([], self.part, None, [None], activates=False))
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["data"]["document"]["title"], "Bolzen")
        self.assertIs(answer["data"]["already_open"], False)


class CloseOnlyIfCleanTests(unittest.TestCase):
    def close(self, app: FakeApp, **args) -> dict:
        with unittest.mock.patch.object(sw_file, "running_app", return_value=app), \
                unittest.mock.patch.object(sw_file, "byref_long", return_value=unittest.mock.Mock(value=0)):
            return sw_file.close_document({"title": r"C:\a\Bolzen.SLDPRT", **args})

    def app(self, dirty: object) -> FakeApp:
        part = FakeDoc("Bolzen", r"C:\a\Bolzen.SLDPRT", dirty=dirty)
        app = FakeApp([part], part, part, [part])
        app.activated = True
        return app

    def test_dirty_or_unreadable_flag_never_reaches_closedoc(self) -> None:
        for dirty, reason in ((True, "dirty"), (1, "dirty"), (RuntimeError("COM"), "unreadable"), (None, "unreadable")):
            with self.subTest(dirty=dirty):
                app = self.app(dirty)
                answer = self.close(app, only_if_clean=True)
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(answer["data"]["reason"], reason)
                self.assertIsNone(answer["data"]["closed"])
                self.assertEqual(answer["data"]["document"], {"title": "Bolzen", "path": r"C:\a\Bolzen.SLDPRT"})
                self.assertEqual(app.closed, [])

    def test_clean_document_is_closed(self) -> None:
        app = self.app(False)
        answer = self.close(app, only_if_clean=True)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["data"]["closed"], app.closed), ("Bolzen", ["Bolzen"]))

    def test_without_the_option_a_dirty_document_is_still_closed(self) -> None:
        app = self.app(True)
        self.assertTrue(self.close(app)["ok"])
        self.assertEqual(app.closed, ["Bolzen"])


class DocumentInfoTests(unittest.TestCase):
    def test_unreadable_save_flag_is_unknown_not_clean(self) -> None:
        self.assertIsNone(sw_core.document_info(FakeDoc("Bolzen", dirty=RuntimeError("COM")))["dirty"])
        self.assertIs(sw_core.document_info(FakeDoc("Bolzen", dirty=False))["dirty"], False)
        self.assertIs(sw_core.document_info(FakeDoc("Bolzen", dirty=True))["dirty"], True)


if __name__ == "__main__":
    unittest.main()
