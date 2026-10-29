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

"""Offline tests for the save path in sw_file.

A save-as onto an existing file must never prompt: the old save path showed a
modal "already exists, replace?" dialog and blocked until someone answered.
With swSaveAsOptions_Silent it returns without one (measured on 2016).  These
tests pin the one silent save path and the absence of an interactive fallback.
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


class FakeExtension:
    """ModelDocExtension whose SaveAs records its arguments and answers as told."""

    def __init__(self, answer: bool = True, errors: int = 0, raises: bool = False) -> None:
        self.answer = answer
        self.errors = errors
        self.raises = raises
        self.calls: list[tuple] = []

    def _FlagAsMethod(self, name: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def SaveAs(self, path, version, options, export_data, errors, warnings):  # noqa: N802 - COM member name
        self.calls.append((path, version, options))
        if self.raises:
            raise RuntimeError("COM error")
        errors.value = self.errors
        return self.answer


class FakeDocument:
    """An unsaved document; its interactive ModelDoc2.SaveAs must never be reached."""

    def __init__(self, extension: FakeExtension, path: str = "") -> None:
        self.Extension = extension
        self.path = path
        self.interactive_calls: list[str] = []

    def GetPathName(self) -> str:  # noqa: N802 - COM member name
        return self.path

    def SaveAs(self, path: str) -> bool:  # noqa: N802 - COM member name
        self.interactive_calls.append(path)
        raise AssertionError("ModelDoc2.SaveAs takes no options and cannot be silenced")


class SilentSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.target = Path(folder.name) / "Teil.sldprt"
        self.target.write_bytes(b"old")   # an existing file: the case that used to prompt

    def test_save_as_passes_the_silent_option(self) -> None:
        ext = FakeExtension()
        doc = FakeDocument(ext)
        self.assertTrue(sw_file._save_as(doc, str(self.target)))
        self.assertEqual(len(ext.calls), 1)
        path, _, options = ext.calls[0]
        self.assertEqual(Path(path), self.target.resolve())
        self.assertTrue(options & sw_file.SW_SAVE_AS_SILENT)
        self.assertEqual(sw_file.SW_SAVE_AS_SILENT, 1)
        self.assertEqual(doc.interactive_calls, [])

    def test_failed_silent_save_does_not_fall_back_to_the_interactive_one(self) -> None:
        for ext in (FakeExtension(answer=False), FakeExtension(answer=True, errors=1), FakeExtension(raises=True)):
            with self.subTest(answer=ext.answer, errors=ext.errors, raises=ext.raises):
                doc = FakeDocument(ext)
                self.assertFalse(sw_file._save_as(doc, str(self.target)))
                self.assertEqual(len(ext.calls), 1)
                self.assertEqual(doc.interactive_calls, [])

    def test_save_and_export_with_overwrite_use_the_silent_path(self) -> None:
        step = self.target.with_suffix(".step")
        step.write_bytes(b"old")
        for tool, target in ((sw_file.save_document, self.target), (sw_file.export_document, step)):
            with self.subTest(tool=tool.__name__):
                ext = FakeExtension()
                doc = FakeDocument(ext)
                with unittest.mock.patch.object(sw_file, "OUTPUT_ROOT", self.target.parent.resolve()), \
                        unittest.mock.patch.object(sw_file, "active_document", return_value=(None, doc)), \
                        unittest.mock.patch.object(sw_file, "clear_selection"):
                    answer = tool({"path": str(target), "overwrite": True})
                self.assertTrue(answer["ok"], answer)
                self.assertEqual([options for _, _, options in ext.calls], [sw_file.SW_SAVE_AS_SILENT])
                self.assertEqual(doc.interactive_calls, [])


class TypedDocument(FakeDocument):
    """A document of a given swDocumentTypes_e: part 1, assembly 2, drawing 3."""

    def __init__(self, extension: FakeExtension, kind: int) -> None:
        super().__init__(extension)
        self.kind = kind

    def GetType(self) -> int:  # noqa: N802 - COM member name
        return self.kind


class PdfExportTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.pdf = self.root / "Zeichnung.pdf"

    def export(self, doc: FakeDocument, path: Path, overwrite: bool = False) -> dict:
        with unittest.mock.patch.object(sw_file, "OUTPUT_ROOT", self.root), \
                unittest.mock.patch.object(sw_file, "active_document", return_value=(None, doc)), \
                unittest.mock.patch.object(sw_file, "clear_selection"):
            return sw_file.export_document({"path": str(path), "overwrite": overwrite})

    def test_a_drawing_exports_to_pdf_through_the_silent_save(self) -> None:
        ext = FakeExtension()
        answer = self.export(TypedDocument(ext, 3), self.pdf)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([(Path(path), options) for path, _, options in ext.calls], [(self.pdf, sw_file.SW_SAVE_AS_SILENT)])

    def test_a_part_or_assembly_is_refused_before_the_path_is_touched(self) -> None:
        for kind in (1, 2):
            with self.subTest(kind=kind):
                ext = FakeExtension()
                target = self.root / "neu" / "Teil.PDF"
                with unittest.mock.patch.object(sw_file, "validated_output_path") as validated:
                    answer = self.export(TypedDocument(ext, kind), target)
                self.assertFalse(answer["ok"])
                self.assertIn("drawings only", answer["message"])
                self.assertEqual(ext.calls, [])
                validated.assert_not_called()
                self.assertFalse(target.parent.exists())

    def test_the_refusal_wins_over_an_existing_target(self) -> None:
        self.pdf.write_bytes(b"old")
        answer = self.export(TypedDocument(FakeExtension(), 1), self.pdf)
        self.assertIn("drawings only", answer["message"])
        self.assertEqual(self.pdf.read_bytes(), b"old")

    def test_an_existing_pdf_needs_overwrite(self) -> None:
        self.pdf.write_bytes(b"old")
        ext = FakeExtension()
        with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
            self.export(TypedDocument(ext, 3), self.pdf)
        self.assertEqual(ext.calls, [])
        answer = self.export(TypedDocument(ext, 3), self.pdf, overwrite=True)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([options for _, _, options in ext.calls], [sw_file.SW_SAVE_AS_SILENT])

    def test_a_pdf_outside_the_output_root_is_refused(self) -> None:
        ext = FakeExtension()
        with self.assertRaisesRegex(RuntimeError, "must be under"):
            self.export(TypedDocument(ext, 3), self.root.parent / "anderswo.pdf")
        self.assertEqual(ext.calls, [])


class FakeSketch:
    """A sketch feature whose Select2 answers as told and records itself in the shared selection."""

    def __init__(self, name: str, selection: list, answer: bool = True) -> None:
        self.Name = name
        self.selection = selection
        self.answer = answer

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def Select2(self, append: bool, mark: int) -> bool:  # noqa: N802 - COM member name
        if self.answer:
            if not append:
                self.selection.clear()
            self.selection.append(self.Name)
        return self.answer


class LoggingExtension(FakeExtension):
    """SaveAs also notes which sketches were selected at the moment of saving."""

    def __init__(self, selection: list) -> None:
        super().__init__()
        self.selection = selection
        self.selected_at_save: list[list[str]] = []

    def SaveAs(self, path, version, options, export_data, errors, warnings):  # noqa: N802 - COM member name
        self.selected_at_save.append(list(self.selection))
        return super().SaveAs(path, version, options, export_data, errors, warnings)


class LibraryFeaturePartTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.profile = self.root / "item" / "profil 8" / "40 x 40.sldlfp"
        self.selection: list[str] = []

    def sketches(self, *names: str, refuse: str = "") -> list[FakeSketch]:
        return [FakeSketch(name, self.selection, answer=name != refuse) for name in names]

    def save(self, doc: FakeDocument, path: Path, sketches: list[FakeSketch], overwrite: bool = False,
             selected: list | None = None) -> dict:
        with unittest.mock.patch.object(sw_file, "OUTPUT_ROOT", self.root),                 unittest.mock.patch.object(sw_file, "active_document", return_value=(None, doc)),                 unittest.mock.patch.object(sw_file, "clear_selection", side_effect=lambda _: self.selection.clear()),                 unittest.mock.patch.object(sw_file, "exit_active_sketch"),                 unittest.mock.patch.object(sw_file, "sketch_features", return_value=sketches),                 unittest.mock.patch.object(sw_file, "selected_objects",
                                           side_effect=lambda _: list(self.selection) if selected is None else selected):
            return sw_file.save_document({"path": str(path), "overwrite": overwrite})

    def test_a_part_saves_as_sldlfp_with_every_2d_sketch_selected(self) -> None:
        self.selection.append("Fremd")   # a leftover selection must not end up in the profile
        ext = LoggingExtension(self.selection)
        answer = self.save(TypedDocument(ext, 1), self.profile, self.sketches("Skizze1", "Skizze2"))
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(Path(answer["data"]["path"]), self.profile)
        self.assertEqual([(Path(path), options) for path, _, options in ext.calls], [(self.profile, sw_file.SW_SAVE_AS_SILENT)])
        self.assertEqual(ext.selected_at_save, [["Skizze1", "Skizze2"]])

    def test_a_plain_save_still_clears_the_selection(self) -> None:
        self.selection.append("Fremd")
        ext = LoggingExtension(self.selection)
        answer = self.save(TypedDocument(ext, 1), self.profile.with_suffix(".sldprt"), self.sketches("Skizze1"))
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(ext.selected_at_save, [[]])

    def test_refusals_before_the_path_is_touched(self) -> None:
        cases = {
            "assembly": (2, self.sketches("Skizze1"), None, "Only a part"),
            "drawing": (3, self.sketches("Skizze1"), None, "Only a part"),
            "part without a 2D sketch": (1, [], None, "has none"),
            "sketch not selectable": (1, self.sketches("Skizze1", "Skizze2", refuse="Skizze2"), None, "Could not select sketch 'Skizze2'"),
            "selection not read back": (1, self.sketches("Skizze1", "Skizze2"), ["Skizze1"], "could not all be selected"),
        }
        for label, (kind, sketches, selected, message) in cases.items():
            with self.subTest(label):
                ext = FakeExtension()
                target = self.root / "neu" / "Profil.SLDLFP"
                with unittest.mock.patch.object(sw_file, "validated_output_path") as validated:
                    answer = self.save(TypedDocument(ext, kind), target, sketches, selected=selected)
                self.assertFalse(answer["ok"])
                self.assertIn(message, answer["message"])
                self.assertEqual(ext.calls, [])
                validated.assert_not_called()
                self.assertFalse(target.parent.exists())

    def test_a_failed_save_is_reported(self) -> None:
        answer = self.save(TypedDocument(FakeExtension(answer=True, errors=1), 1), self.profile, self.sketches("Skizze1"))
        self.assertFalse(answer["ok"])

    def test_an_existing_profile_needs_overwrite(self) -> None:
        self.profile.parent.mkdir(parents=True)
        self.profile.write_bytes(b"old")
        ext = FakeExtension()
        with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
            self.save(TypedDocument(ext, 1), self.profile, self.sketches("Skizze1"))
        self.assertEqual(ext.calls, [])
        self.assertTrue(self.save(TypedDocument(ext, 1), self.profile, self.sketches("Skizze1"), overwrite=True)["ok"])

    def test_a_profile_outside_the_output_root_is_refused(self) -> None:
        ext = FakeExtension()
        with self.assertRaisesRegex(RuntimeError, "must be under"):
            self.save(TypedDocument(ext, 1), self.root.parent / "weldment profiles" / "x" / "y" / "z.sldlfp", self.sketches("Skizze1"))
        self.assertEqual(ext.calls, [])

if __name__ == "__main__":
    unittest.main()
