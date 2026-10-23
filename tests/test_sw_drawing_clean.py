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

"""Offline tests for real section views and dimension clean-up (issue #2, d-veloping/ClauSW#157).

The fakes follow what SOLIDWORKS 2016 SP3 did in the ClauSW spike: a view's own
sketch takes (sheet - position) / scale, CreateSectionViewAt5 returns a section
view also for a line beside the part (then without hatched faces), hidden
dimensions drop out of the GetFirstDisplayDimension5 walk, and drawing
dimensions are selected by name.
"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_core, sw_drawing


class Transform:
    def __init__(self, data: list[float]) -> None:
        self.ArrayData = data


class Dimension:
    def __init__(self, full_name: str, value_m: float, kind: int = 1) -> None:   # swDimensionParamType_e
        self.FullName = full_name
        self.SystemValue = value_m
        self.kind = kind

    def GetType(self) -> int:  # noqa: N802 - COM member name
        return self.kind


class Annotation:
    def __init__(self, x: float, y: float) -> None:
        self.Visible = 1
        self.Layer = ""
        self.position = (x, y, 0.0)

    def GetPosition(self):  # noqa: N802 - COM member name
        return self.position


class DisplayDimension:
    def __init__(self, view: "View", dimension: Dimension, x_mm: float, y_mm: float, kind2: int = 12) -> None:
        self.view = view
        self.dimension = dimension
        self.annotation = Annotation(x_mm / 1000, y_mm / 1000)
        self.Type2 = kind2
        self.ShowParenthesis = False

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def GetDimension2(self, index: int) -> Dimension:  # noqa: N802 - COM member name
        return self.dimension

    def GetAnnotation(self) -> Annotation:  # noqa: N802 - COM member name
        return self.annotation

    def GetNameForSelection(self) -> str:  # noqa: N802 - COM member name
        return "" if getattr(self, "unselectable", False) else f"{self.dimension.FullName}@{self.view.name}"

    def GetNext5(self):  # noqa: N802 - COM member name
        shown = self.view.shown()
        index = shown.index(self)
        return shown[index + 1] if index + 1 < len(shown) else None


class View:
    def __init__(self, name: str, kind: int = 7, position=(0.2, 0.15), scale: float = 0.5,
                 outline=(0.15, 0.1, 0.25, 0.2), hatches: int = 0, transform=None) -> None:
        self.name = name
        self.Type = kind
        self.Position = position
        self.Angle = 0.0
        self.ScaleDecimal = scale
        self.outline = outline
        self.hatches = hatches
        self.section_lines: list[object] = []
        self.transform = transform or Transform([1, 0, 0, 0, 1, 0, 0, 0, 1, 0.2, 0.15, 0, scale, 0, 0, 0])
        self.next = None
        self.displays: list[DisplayDimension] = []
        self.section = None

    def shown(self) -> list[DisplayDimension]:
        return [d for d in self.displays if d.annotation.Visible == 1]

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def GetName2(self) -> str:  # noqa: N802 - COM member name
        return self.name

    def GetNextView(self):  # noqa: N802 - COM member name
        return self.next

    def GetDisplayMode2(self) -> int:  # noqa: N802 - COM member name
        return 2

    def GetOutline(self):  # noqa: N802 - COM member name
        return self.outline

    def GetFaceHatchCount(self) -> int:  # noqa: N802 - COM member name
        return self.hatches

    def GetSectionLines(self):  # noqa: N802 - COM member name
        return tuple(self.section_lines)

    @property
    def ModelToViewTransform(self):  # noqa: N802 - COM member name
        return self.transform

    def GetFirstDisplayDimension5(self):  # noqa: N802 - COM member name
        shown = self.shown()
        return shown[0] if shown else None

    def GetSection(self):  # noqa: N802 - COM member name
        return self.section


class SketchManager:
    def __init__(self) -> None:
        self.lines: list[tuple] = []
        self.circles: list[tuple] = []

    def CreateLine(self, *coords):  # noqa: N802 - COM member name
        self.lines.append(coords)
        return object()

    def CreateCircleByRadius(self, *coords):  # noqa: N802 - COM member name
        self.circles.append(coords)
        return object()


class SectionLine:
    def __init__(self, name: str, label: str = "A") -> None:
        self.name = name
        self.label = label
        self.view: "View | None" = None

    def GetName(self) -> str:  # noqa: N802 - COM member name
        return self.name

    def GetLabel(self) -> str:  # noqa: N802 - COM member name
        return self.label

    def GetSectionView(self):  # noqa: N802 - COM member name
        return self.view


class Extension:
    def __init__(self, doc: "Drawing") -> None:
        self.doc = doc
        self.deleted = 0
        self.no_effect: set[type] = set()   # DeleteSelection2 says True and leaves these kinds where they are
        self.align_moves = True

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def SelectByID2(self, name, kind, x, y, z, append, mark, callout, option):  # noqa: N802 - COM member name
        if not append:
            self.doc.selection.clear()
        if kind == "DRAWINGVIEW":
            hit = [v for v in self.doc.views if v.name == name]
        elif kind == "SECTIONLINE":
            hit = [line for v in self.doc.views for line in v.section_lines if line.name == name]
        else:
            # like SOLIDWORKS is assumed to: only what is on the active sheet can be selected
            shown = self.doc.views if self.doc.current == "Sheet1" else self.doc.other_sheet
            hit = [d for v in shown for d in v.displays if name and d.GetNameForSelection() == name]
        self.doc.selection.extend(hit)
        return bool(hit)

    def AlignDimensions(self, kind, spacing):  # noqa: N802 - COM member name
        self.doc.aligned.append((kind, round(spacing, 6), len(self.doc.selection)))
        if self.align_moves:
            for item in self.doc.selection:
                x, y, z = item.annotation.position
                item.annotation.position = (x, y - 0.006, z)
        return True

    def DeleteSelection2(self, options):  # noqa: N802 - COM member name
        for item in list(self.doc.selection):
            if type(item) in self.no_effect:
                continue
            if isinstance(item, View):
                self.doc.views.remove(item)
            elif isinstance(item, SectionLine):
                for v in self.doc.views:
                    if item in v.section_lines:
                        v.section_lines.remove(item)
            else:
                item.view.displays.remove(item)
        self.deleted += len(self.doc.selection)
        self.doc.selection.clear()
        return True


class Drawing:
    def __init__(self, views: list[View]) -> None:
        self.views = views
        self.selection: list[object] = []
        self.sketch = SketchManager()
        self.extension = Extension(self)
        self.section_calls: list[tuple] = []
        self.next_section: View | None = None
        self.hidden_calls = 0
        self.aligned: list[tuple] = []
        self.other_sheet: list[View] = []   # views of a second sheet: GetViews lists them, the view chain does not
        self.current = "Sheet1"
        self.sizes = {"Sheet1": (0.42, 0.297), "Sheet2": (0.42, 0.297)}
        self.activated: list[str] = []

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def GetFirstView(self):  # noqa: N802 - COM member name
        for a, b in zip(self.views, self.views[1:] + [None]):
            a.next = b
        return self.views[0]

    def GetViews(self):  # noqa: N802 - COM member name
        sheets = [tuple(self.views)]
        if self.other_sheet:
            sheets.append((View("Sheet2", kind=1), *self.other_sheet))
        return tuple(sheets)

    def Sheet(self, name):  # noqa: N802 - COM member name
        return Sheet(name, self.sizes[name])

    def ActivateSheet(self, name):  # noqa: N802 - COM member name
        self.activated.append(name)
        self.current = name
        return True

    def ActivateView(self, name: str) -> bool:  # noqa: N802 - COM member name
        self.active = next((v for v in self.views if v.name == name), None)
        return self.active is not None

    def CreateSectionViewAt5(self, x, y, z, label, options, excluded, depth):  # noqa: N802 - COM member name
        self.section_calls.append((x, y, label, options, depth))
        if self.next_section is None and not getattr(self, "null_makes_line", True):
            return None   # no view and the drawn line stays a sketch line
        # SOLIDWORKS turns the selected line into a section line of the active view, cut or not
        line = SectionLine(f"Section Line{len(self.section_calls)}", label)
        self.active.section_lines.append(line)
        view = self.next_section
        if view is not None:
            view.section = line
            line.view = view
            self.views.append(view)
        return view

    def HideDimension(self) -> None:  # noqa: N802 - COM member name
        self.hidden_calls += 1
        for item in self.selection:
            item.annotation.Visible = 3

    def GetCurrentSheet(self):  # noqa: N802 - COM member name
        return Sheet(self.current, self.sizes[self.current])


class Sheet:
    def __init__(self, name: str = "Sheet1", size=(0.42, 0.297)) -> None:
        self.name, self.size = name, size

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def GetName(self) -> str:  # noqa: N802 - COM member name
        return self.name

    def GetProperties2(self):  # noqa: N802 - COM member name
        return (8, 0, 1, 1, True, *self.size)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.sheet = View("Sheet1", kind=1)
        self.parent = View("Drawing View1")
        self.doc = Drawing([self.sheet, self.parent])
        for patch in (
            unittest.mock.patch.object(sw_drawing, "require_drawing", return_value=(None, self.doc)),
            unittest.mock.patch.object(sw_drawing, "rebuild"),
            unittest.mock.patch.object(sw_drawing, "clear_selection", side_effect=lambda d: d.selection.clear()),
            unittest.mock.patch.object(sw_drawing, "sketch_manager", return_value=self.doc.sketch),
            unittest.mock.patch.object(sw_drawing, "extension", side_effect=lambda d: d.extension),
            unittest.mock.patch.object(sw_drawing, "nothing", return_value=None),
            unittest.mock.patch.object(sw_core, "select_object", return_value=True),
            unittest.mock.patch.object(sw_core, "empty_variant", return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)


class SectionViewTests(Base):
    def test_sheet_points_go_into_the_view_sketch(self) -> None:
        # view at (200, 150) mm, scale 1:2: the sheet point (210, 140) is (20, -20) mm in the view's sketch
        x, y = sw_drawing._sketch_point(self.parent, 210.0, 140.0)
        self.assertAlmostEqual(x, 0.02)
        self.assertAlmostEqual(y, -0.02)

    def test_a_model_point_maps_through_the_view_transform(self) -> None:
        # front view: model x to sheet x, model y to sheet y, half scale, origin at (200, 150)
        self.assertEqual(tuple(round(c, 6) for c in sw_drawing._sheet_point(self.parent, [40.0, -20.0, 7.0])),
                         (220.0, 140.0))
        # a side view whose sheet x is model -z: rows are the images of the model axes (p·R)
        side = View("V", transform=Transform([0, 0, 1, 0, 1, 0, -1, 0, 0, 0.3, 0.15, 0, 1.0, 0, 0, 0]))
        self.assertEqual(tuple(round(c, 6) for c in sw_drawing._sheet_point(side, [0.0, 10.0, 25.0])), (275.0, 160.0))

    def test_a_cut_with_hatched_faces_succeeds_with_option_0(self) -> None:
        self.doc.next_section = View("Section View A-A", kind=2, hatches=3)
        answer = sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210,
                                                 "place_x_mm": 80, "place_y_mm": 150, "parent_view": "Drawing View1"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["data"]["view"]["face_hatches"], 3)
        line = self.doc.sketch.lines[0]
        self.assertEqual([round(c, 6) for c in line], [0.0, -0.12, 0.0, 0.0, 0.12, 0.0])
        self.assertEqual(self.doc.section_calls[0][3], 0)

    def test_reverse_and_exclude_fasteners_use_the_type_library_bits(self) -> None:
        self.doc.next_section = View("Section View A-A", kind=2, hatches=1)
        sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210, "place_x_mm": 80,
                                        "place_y_mm": 150, "reverse": True, "exclude_fasteners": True})
        self.assertEqual(self.doc.section_calls[0][3], 4 | 64)

    def test_through_a_model_point_spans_the_parent_outline(self) -> None:
        self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
        answer = sw_drawing.insert_section_view({"through_model_mm": [20, 0, 0], "direction": "vertical",
                                                 "place_x_mm": 80, "place_y_mm": 150})
        self.assertTrue(answer["ok"], answer)
        # model x 20 → sheet x 210; outline y 100..200 plus 5 mm each side
        self.assertEqual(answer["data"]["line_mm"], [210.0, 95.0, 210.0, 205.0])
        self.assertEqual([round(c, 6) for c in self.doc.sketch.lines[0]], [0.02, -0.11, 0.0, 0.02, 0.11, 0.0])

    def test_a_view_that_cut_nothing_is_deleted_and_fails(self) -> None:
        empty = View("Section View A-A", kind=2, hatches=0)
        self.doc.next_section = empty
        answer = sw_drawing.insert_section_view({"x1_mm": 400, "y1_mm": 10, "x2_mm": 400, "y2_mm": 20,
                                                 "place_x_mm": 80, "place_y_mm": 150})
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["data"]["removed"])
        self.assertTrue(answer["data"]["line_removed"])
        self.assertNotIn(empty, self.doc.views)
        self.assertEqual(self.parent.section_lines, [])
        self.assertIn("cut nothing", answer["message"])

    def test_an_earlier_section_line_survives_a_failed_cut(self) -> None:
        self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
        sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210, "place_x_mm": 80,
                                        "place_y_mm": 150, "parent_view": "Drawing View1"})
        self.doc.next_section = View("Section View B-B", kind=2, hatches=0)
        answer = sw_drawing.insert_section_view({"x1_mm": 400, "y1_mm": 10, "x2_mm": 400, "y2_mm": 20, "label": "B",
                                                 "place_x_mm": 80, "place_y_mm": 50, "parent_view": "Drawing View1"})
        self.assertFalse(answer["ok"])
        self.assertEqual([line.name for line in self.parent.section_lines], ["Section Line1"])
        self.assertEqual(answer["data"]["parent_section_lines"], 1)

    def test_no_view_at_all_fails_and_reads_the_parent_back(self) -> None:
        self.doc.next_section = None   # CreateSectionViewAt5 returns null, the line became a cutting line anyway
        answer = sw_drawing.insert_section_view({"x1_mm": 400, "y1_mm": 10, "x2_mm": 400, "y2_mm": 20,
                                                 "place_x_mm": 80, "place_y_mm": 50, "parent_view": "Drawing View1"})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["parent_section_lines"], 1)
        self.assertIn("may still be in the parent view", answer["message"])

    def test_a_view_that_cut_nothing_and_whose_line_stays_says_so(self) -> None:
        # SOLIDWORKS answers True for the deletion of the cutting line and leaves it
        self.doc.extension.no_effect = {SectionLine}
        self.doc.next_section = View("Section View A-A", kind=2, hatches=0)
        answer = sw_drawing.insert_section_view({"x1_mm": 400, "y1_mm": 10, "x2_mm": 400, "y2_mm": 20,
                                                 "place_x_mm": 80, "place_y_mm": 150})
        self.assertFalse(answer["ok"])
        self.assertEqual((answer["data"]["removed"], answer["data"]["line_removed"]), (True, False))
        self.assertEqual(answer["data"]["parent_section_lines"], 1)
        self.assertIn("cutting line is still in the parent view", answer["message"])

    def test_a_view_that_stays_although_deletion_said_true_fails(self) -> None:
        self.doc.extension.no_effect = {View}
        self.doc.next_section = View("Section View A-A", kind=2, hatches=0)
        answer = sw_drawing.insert_section_view({"x1_mm": 400, "y1_mm": 10, "x2_mm": 400, "y2_mm": 20,
                                                 "place_x_mm": 80, "place_y_mm": 150})
        self.assertIs(answer["data"]["removed"], False)
        self.assertIn("could not be deleted and is still on the sheet", answer["message"])

    def test_delete_trusts_the_drawing_not_the_answer(self) -> None:
        self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
        sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210, "place_x_mm": 80,
                                        "place_y_mm": 150, "parent_view": "Drawing View1"})
        for kinds, removed, line_removed in (({SectionLine}, True, False), ({View}, False, True)):
            with self.subTest(stays=kinds):
                self.setUp()
                self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
                sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210,
                                                "place_x_mm": 80, "place_y_mm": 150, "parent_view": "Drawing View1"})
                self.doc.extension.no_effect = kinds
                answer = sw_drawing.delete_drawing_view({"name": "Section View A-A"})
                self.assertFalse(answer["ok"])
                self.assertEqual((answer["data"]["removed"], answer["data"]["line_removed"]), (removed, line_removed))

    def test_a_view_that_stays_with_an_unreadable_name_is_not_gone(self) -> None:
        # the deletion takes nothing away, and afterwards the name of the view that stayed cannot be read
        self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
        sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210, "place_x_mm": 80,
                                        "place_y_mm": 150, "parent_view": "Drawing View1"})
        section = self.doc.views[-1]
        self.doc.extension.no_effect = {View, SectionLine}
        echt = self.doc.extension.DeleteSelection2

        def loeschen(options):
            def refused():
                raise OSError("COM")
            section.GetName2 = refused
            return echt(options)
        self.doc.extension.DeleteSelection2 = loeschen
        answer = sw_drawing.delete_drawing_view({"name": "Section View A-A"})
        self.assertFalse(answer["ok"])
        self.assertIs(answer["data"]["readable"], False)

    def test_a_section_view_whose_line_is_not_found_is_not_deleted_completely(self) -> None:
        self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
        sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210, "place_x_mm": 80,
                                        "place_y_mm": 150, "parent_view": "Drawing View1"})
        self.parent.section_lines[0].view = None   # the line no longer names its section view
        answer = sw_drawing.delete_drawing_view({"name": "Section View A-A"})
        self.assertFalse(answer["ok"])
        self.assertEqual((answer["data"]["removed"], answer["data"]["line_removed"]), (True, None))
        self.assertEqual(len(self.parent.section_lines), 1)

    def test_sheet_point_without_a_transform_fails(self) -> None:
        self.parent.transform = Transform([1, 0, 0])
        answer = sw_drawing.insert_section_view({"through_model_mm": [20, 0, 0], "place_x_mm": 80, "place_y_mm": 150})
        self.assertFalse(answer["ok"])
        self.assertIn("does not report its model transform", answer["message"])
        self.assertEqual((self.doc.sketch.lines, self.doc.section_calls), ([], []))

    def test_unreadable_cutting_lines_fail_instead_of_counting_as_none(self) -> None:
        self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
        sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210, "place_x_mm": 80,
                                        "place_y_mm": 150, "parent_view": "Drawing View1"})

        def refused():
            raise OSError("COM")
        self.parent.GetSectionLines = refused
        answer = sw_drawing.delete_drawing_view({"name": "Section View A-A"})
        self.assertFalse(answer["ok"])
        self.assertIs(answer["data"]["readable"], False)
        self.assertIn("Section View A-A", [v.name for v in self.doc.views])   # nothing deleted
        listed = sw_drawing.list_drawing_views({})["data"]["views"]
        self.assertIsNone(next(v for v in listed if v["name"] == "Drawing View1")["section_lines"])

    def test_an_unreadable_view_chain_fails(self) -> None:
        def refused():
            raise OSError("COM")
        self.parent.GetNextView = refused
        for call in (lambda: sw_drawing.list_drawing_views({}),
                     lambda: sw_drawing.delete_drawing_view({"name": "Drawing View1"})):
            answer = call()
            self.assertFalse(answer["ok"])
            self.assertIn("could not read GetNextView", answer["message"])

    def test_unreadable_position_or_a_rotated_parent_draws_nothing(self) -> None:
        for change, expected in ((lambda v: setattr(v, "Position", None), "no usable position"),
                                 (lambda v: setattr(v, "ScaleDecimal", 0.0), "no usable scale"),
                                 (lambda v: setattr(v, "Angle", 0.5), "is rotated")):
            with self.subTest(expected=expected):
                self.setUp()
                change(self.parent)
                self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
                answer = sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210,
                                                         "place_x_mm": 80, "place_y_mm": 150,
                                                         "parent_view": "Drawing View1"})
                self.assertFalse(answer["ok"])
                self.assertIn(expected, answer["message"])
                self.assertEqual((self.doc.sketch.lines, self.doc.section_calls), ([], []))

    def test_deleting_a_section_view_takes_its_cutting_line(self) -> None:
        self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
        sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210, "place_x_mm": 80,
                                        "place_y_mm": 150, "parent_view": "Drawing View1"})
        self.assertEqual(len(self.parent.section_lines), 1)
        answer = sw_drawing.delete_drawing_view({"name": "Section View A-A"})
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["data"]["line_removed"])
        self.assertEqual(self.parent.section_lines, [])
        self.assertFalse(sw_drawing.delete_drawing_view({"name": "Section View A-A"})["ok"])

    def test_a_selected_line_without_parent_view_is_cleaned_up_too(self) -> None:
        self.doc.ActivateView("Drawing View1")
        self.doc.next_section = View("Section View A-A", kind=2, hatches=0)
        answer = sw_drawing.insert_section_view({"use_selection": True, "place_x_mm": 80, "place_y_mm": 150})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["parent"], "Drawing View1")
        self.assertTrue(answer["data"]["line_removed"])
        self.assertEqual(self.parent.section_lines, [])

    def test_a_label_in_use_is_refused_before_anything_is_drawn(self) -> None:
        self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
        sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210, "place_x_mm": 80,
                                        "place_y_mm": 150, "parent_view": "Drawing View1"})
        answer = sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210,
                                                 "place_x_mm": 80, "place_y_mm": 50, "label": "A"})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["labels_in_use"], ["A"])
        self.assertEqual(len(self.doc.sketch.lines), 1)

    def test_a_selected_line_without_parent_view_names_the_parent(self) -> None:
        self.doc.ActivateView("Drawing View1")
        self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
        answer = sw_drawing.insert_section_view({"use_selection": True, "place_x_mm": 80, "place_y_mm": 150})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["data"]["parent"], answer["data"]["parent_section_lines"]), ("Drawing View1", 1))

    def test_bad_model_point_is_refused(self) -> None:
        answer = sw_drawing.insert_section_view({"through_model_mm": [1, 2], "place_x_mm": 80, "place_y_mm": 150})
        self.assertFalse(answer["ok"])
        self.assertEqual(self.doc.section_calls, [])

    def test_detail_circle_is_converted_too(self) -> None:
        with unittest.mock.patch.object(sw_drawing, "call_versioned", return_value=View("Detail View I", kind=3)):
            answer = sw_drawing.insert_detail_view({"center_x_mm": 210, "center_y_mm": 150, "radius_mm": 5,
                                                    "place_x_mm": 350, "place_y_mm": 60})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([round(c, 6) for c in self.doc.sketch.circles[0]], [0.02, 0.0, 0.0, 0.01])


class DimensionTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.section = View("Section View A-A", kind=2, hatches=3)
        self.doc.views.append(self.section)
        d1 = Dimension("D1@Skizze1@Teil.Part", 0.0304)
        d2 = Dimension("D2@Skizze1@Teil.Part", 0.012)
        self.section.displays = [DisplayDimension(self.section, d1, 100, 150), DisplayDimension(self.section, d2, 120, 150)]
        self.parent.displays = [DisplayDimension(self.parent, d1, 230, 150), DisplayDimension(self.parent, d2, 500, 150)]

    def test_list_names_duplicates_and_text_outside_the_sheet(self) -> None:
        answer = sw_drawing.list_drawing_dimensions({})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(len(answer["data"]["dimensions"]), 4)
        self.assertEqual(answer["data"]["duplicates"]["D1@Skizze1@Teil.Part"], ["Drawing View1", "Section View A-A"])
        self.assertEqual(answer["data"]["outside_sheet"], 1)
        first = answer["data"]["dimensions"][0]
        self.assertEqual(first["value_mm"], 30.4)
        self.assertIs(first["parentheses"], False)
        self.assertTrue(first["visible"])

    def test_a_dimension_on_a_hidden_layer_is_left_out(self) -> None:
        class Layer:
            def __init__(self, visible: bool) -> None:
                self.Visible = visible

        class Layers:
            def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
                pass

            def GetLayer(self, name):  # noqa: N802 - COM member name
                return Layer(name != "Versteckt")
        self.doc.GetLayerManager = lambda: Layers()
        self.parent.displays[1].annotation.Layer = "Versteckt"
        self.parent.displays[0].annotation.Layer = "Maße"
        listed = sw_drawing.list_drawing_dimensions({})["data"]["dimensions"]
        self.assertEqual(len(listed), 3)
        self.assertIn("Maße", [d["layer"] for d in listed])
        self.assertTrue(sw_drawing.arrange_drawing_dimensions({"view_name": "Drawing View1"})["ok"])

    def test_an_unreadable_layer_or_visibility_is_listed_as_unknown(self) -> None:
        class Layer:
            Visible = None

        class Layers:
            def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
                pass

            def GetLayer(self, name):  # noqa: N802 - COM member name
                if name == "Kaputt":
                    raise OSError("no such layer")
                return None if name == "-Per Standard-" else Layer()
        self.doc.GetLayerManager = lambda: Layers()
        for display, layer in zip(self.parent.displays, ("Kaputt", "-Per Standard-")):
            display.annotation.Layer = layer
        self.section.displays[0].annotation.Layer = "Ohne Sichtbarkeit"
        self.section.displays[1].annotation.Visible = None   # IAnnotation.Visible not readable
        unittest.mock.patch.object(View, "shown", lambda view: list(view.displays)).start()
        self.addCleanup(unittest.mock.patch.stopall)
        listed = sw_drawing.list_drawing_dimensions({})["data"]
        # nothing drops out, nothing unknown passes for visible: only the default layer shows for sure; the model
        # dimensions behind all four stay readable
        self.assertEqual([(d["view"], d["layer"], d["visible"], d["readable"]) for d in listed["dimensions"]],
                         [("Drawing View1", "Kaputt", None, True), ("Drawing View1", "-Per Standard-", True, True),
                          ("Section View A-A", "Ohne Sichtbarkeit", None, True), ("Section View A-A", None, None, True)])
        self.assertEqual(listed["duplicates"], {})   # D1 and D2 each show for sure in one view only
        # an arrangement could select a hidden one: refused, nothing selected
        answer = sw_drawing.arrange_drawing_dimensions({"view_name": "Section View A-A"})
        self.assertFalse(answer["ok"])
        self.assertEqual((answer["data"]["visibility_unknown"], self.doc.aligned), (2, []))
        # one unknown among others is enough, and nothing is selected
        answer = sw_drawing.arrange_drawing_dimensions({"view_name": "Drawing View1"})
        self.assertFalse(answer["ok"])
        self.assertEqual((answer["data"]["visibility_unknown"], answer["data"]["selected"], self.doc.aligned,
                          self.doc.selection), (1, 0, [], []))
        # hiding cannot prove its targets gone while the view is unreadable; one of unknown visibility that stays is
        # not reported as still visible
        self.parent.displays[0].unselectable = True
        answer = sw_drawing.hide_drawing_dimensions({"dimensions": [
            {"view": "Drawing View1", "full_name": "D1@Skizze1@Teil.Part"}]})
        self.assertFalse(answer["ok"])
        self.assertEqual((answer["data"]["remaining"], answer["data"]["unreadable_views"]), ([], ["Drawing View1"]))
        self.parent.displays[0].unselectable = False
        answer = sw_drawing.hide_drawing_dimensions({"dimensions": [
            {"view": "Drawing View1", "full_name": "D2@Skizze1@Teil.Part"}]})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["unreadable_views"], ["Drawing View1"])

    def test_a_layer_that_cannot_be_read_leaves_visibility_unknown(self) -> None:
        class NoLayer(Annotation):
            @property
            def Layer(self):  # noqa: N802 - COM member name
                raise OSError("Layer not readable")

            @Layer.setter
            def Layer(self, name) -> None:  # noqa: N802 - COM member name
                pass
        self.parent.displays[0].annotation = NoLayer(0.23, 0.15)
        listed = sw_drawing.list_drawing_dimensions({"view_name": "Drawing View1"})["data"]["dimensions"]
        self.assertEqual([(d["layer"], d["visible"]) for d in listed], [(None, None), (None, True)])

    def test_a_hidden_dimension_in_the_walk_is_left_out(self) -> None:
        # documented behaviour: the walk also returns suppressed dimensions
        self.parent.displays[1].annotation.Visible = 3
        with unittest.mock.patch.object(View, "shown", lambda view: list(view.displays)):
            listed = sw_drawing.list_drawing_dimensions({})["data"]["dimensions"]
            self.assertEqual(len(listed), 3)
            self.assertTrue(all(d["visible"] for d in listed))
            answer = sw_drawing.arrange_drawing_dimensions({"view_name": "Drawing View1"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["data"]["selected"], 1)

    def test_hiding_an_already_hidden_dimension_is_not_on_the_drawing(self) -> None:
        self.parent.displays[0].annotation.Visible = 3
        answer = sw_drawing.hide_drawing_dimensions({"dimensions": [
            {"view": "Drawing View1", "full_name": "D1@Skizze1@Teil.Part"}]})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["missing"], ["Drawing View1: D1@Skizze1@Teil.Part"])
        self.assertEqual(self.doc.hidden_calls, 0)

    def second_sheet(self, size=(0.42, 0.297)) -> View:
        second = View("Drawing View5")
        second.displays = [DisplayDimension(second, Dimension("D1@Skizze1@Teil.Part", 0.0304), 350, 100)]
        self.doc.other_sheet = [second]
        self.doc.sizes["Sheet2"] = size
        return second

    def test_dimensions_on_a_second_sheet_are_listed_with_their_sheet(self) -> None:
        self.second_sheet()
        listed = sw_drawing.list_drawing_dimensions({})["data"]
        self.assertEqual(len(listed["dimensions"]), 5)
        self.assertEqual([d["sheet"] for d in listed["dimensions"]], ["Sheet1"] * 4 + ["Sheet2"])
        # a repeat on another sheet is no duplicate
        self.assertEqual(listed["duplicates"], {f"D{i}@Skizze1@Teil.Part @ Sheet1": ["Drawing View1", "Section View A-A"]
                                                for i in (1, 2)})
        self.assertTrue(sw_drawing.list_drawing_dimensions({"view_name": "Drawing View5"})["ok"])
        self.assertFalse(sw_drawing.list_drawing_dimensions({"view_name": "Sheet2"})["ok"])

    def test_outside_is_measured_against_the_own_sheet(self) -> None:
        self.second_sheet(size=(0.297, 0.21))   # A4: the text at x = 350 mm lies outside
        listed = sw_drawing.list_drawing_dimensions({})["data"]
        # Drawing View1 holds one text at x = 500 mm (outside A3), Drawing View5 one at x = 350 mm (outside A4)
        self.assertEqual([d["outside_sheet"] for d in listed["dimensions"]], [False, True, False, False, True])
        self.assertEqual(listed["outside_sheet"], 2)

    def test_hide_on_another_sheet_activates_it_and_back(self) -> None:
        self.second_sheet()
        answer = sw_drawing.hide_drawing_dimensions({"dimensions": [{"view": "Drawing View5", "full_name": "D1@Skizze1@Teil.Part"}]})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.doc.activated, ["Sheet2", "Sheet1"])
        self.assertEqual(self.doc.current, "Sheet1")
        self.assertEqual(self.doc.selection, [])

    def test_arrange_on_another_sheet(self) -> None:
        self.second_sheet()
        answer = sw_drawing.arrange_drawing_dimensions({"view_name": "Drawing View5"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.doc.aligned, [(0, 0.006, 1)])
        self.assertEqual(self.doc.activated, ["Sheet2", "Sheet1"])

    def test_bare_dispatch_pointers_are_wrapped(self) -> None:
        class PyIDispatch:   # what GetViews hands back on 2016 SP3: nothing readable until wrapped
            def __init__(self, inner) -> None:
                self.inner = inner
        views = list(self.doc.views)
        self.doc.GetViews = lambda: (tuple(PyIDispatch(v) for v in views),)
        with unittest.mock.patch("win32com.client.Dispatch", side_effect=lambda o: o.inner):
            listed = sw_drawing.list_drawing_dimensions({})["data"]
        self.assertEqual(len(listed["dimensions"]), 4)

    def test_an_endless_dimension_walk_is_an_error(self) -> None:
        longest = max(len(sw_drawing._display_dimensions(v)) for v in self.doc.views)
        self.assertGreater(longest, 1)
        # a walk of exactly the limit ends normally, one more is an error
        with unittest.mock.patch.object(sw_drawing, "DIMENSION_WALK_LIMIT", longest):
            self.assertTrue(sw_drawing.list_drawing_dimensions({})["ok"])
        with unittest.mock.patch.object(sw_drawing, "DIMENSION_WALK_LIMIT", longest - 1):
            with self.assertRaisesRegex(RuntimeError, "did not end"):
                sw_drawing.list_drawing_dimensions({})

    def test_no_sheets_listed_is_an_error(self) -> None:
        self.doc.GetViews = lambda: None
        with self.assertRaisesRegex(RuntimeError, "GetViews"):
            sw_drawing.list_drawing_dimensions({})

    def test_arrange_selects_every_dimension_or_nothing(self) -> None:
        answer = sw_drawing.arrange_drawing_dimensions({"view_name": "Section View A-A", "spacing_mm": 6})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.doc.aligned, [(0, 0.006, 2)])
        self.section.displays[1].unselectable = True
        answer = sw_drawing.arrange_drawing_dimensions({"view_name": "Section View A-A"})
        self.assertFalse(answer["ok"])
        self.assertEqual(len(self.doc.aligned), 1)
        self.assertEqual((answer["data"]["arranged"], answer["data"]["selected"]), (0, 1))
        self.assertIn("1 of 2 could be selected", answer["message"])

    def test_hide_in_one_call_leaves_one_of_each(self) -> None:
        answer = sw_drawing.hide_drawing_dimensions({"dimensions": [
            {"view": "Drawing View1", "full_name": "D1@Skizze1@Teil.Part"},
            {"view": "Drawing View1", "full_name": "D2@Skizze1@Teil.Part"}]})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.doc.hidden_calls, 1)
        self.assertEqual(answer["data"]["selected"], 2)
        self.assertEqual(answer["data"]["duplicates"], {})
        self.assertEqual(answer["data"]["dimensions_left"], 2)

    def test_an_unreadable_walk_fails_and_never_proves_hidden(self) -> None:
        def refused():
            raise OSError("COM")
        answer = sw_drawing.hide_drawing_dimensions({"dimensions": [
            {"view": "Drawing View1", "full_name": "D1@Skizze1@Teil.Part"}]})
        self.assertTrue(answer["ok"], answer)
        self.parent.GetFirstDisplayDimension5 = refused
        for call in (lambda: sw_drawing.list_drawing_dimensions({}),
                     lambda: sw_drawing.hide_drawing_dimensions({"dimensions": [
                         {"view": "Drawing View1", "full_name": "D2@Skizze1@Teil.Part"}]})):
            answer = call()
            self.assertFalse(answer["ok"])
            self.assertIn("GetFirstDisplayDimension5", answer["message"])

    def test_an_unreadable_model_dimension_is_marked_and_blocks_proof(self) -> None:
        self.parent.displays[1].dimension = Dimension("D2@Skizze1@Teil.Part", None)
        listed = sw_drawing.list_drawing_dimensions({})["data"]["dimensions"]
        broken = [d for d in listed if not d["readable"]]
        self.assertEqual(len(broken), 1)
        self.assertNotIn("full_name", broken[0])
        self.assertFalse({"value_mm", "value_deg"} & set(broken[0]))
        answer = sw_drawing.hide_drawing_dimensions({"dimensions": [
            {"view": "Drawing View1", "full_name": "D1@Skizze1@Teil.Part"}]})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["unreadable_views"], ["Drawing View1"])
        # it still shows for sure: the view is arranged with it (the only one left there), by its selection name
        answer = sw_drawing.arrange_drawing_dimensions({"view_name": "Drawing View1"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["data"]["selected"], self.doc.aligned[-1][2]), (1, 1))

    def test_dimension_kinds_follow_the_parameter_type(self) -> None:
        import math
        self.section.displays[0].dimension = Dimension("A1@Skizze1@Teil.Part", math.radians(30), kind=2)
        self.section.displays[1].dimension = Dimension("D1@LPattern1@Teil.Part", 4, kind=3)
        listed = sw_drawing.list_drawing_dimensions({"view_name": "Section View A-A"})["data"]["dimensions"]
        self.assertAlmostEqual(listed[0]["value_deg"], 30.0)
        self.assertEqual((listed[1]["value"], "value_mm" in listed[1]), (4, False))

    def test_going_back_to_the_previous_sheet_is_checked(self) -> None:
        self.second_sheet()
        self.doc.ActivateSheet = lambda name: (setattr(self.doc, "current", name) or name == "Sheet2")
        answer = sw_drawing.hide_drawing_dimensions({"dimensions": [{"view": "Drawing View5",
                                                                     "full_name": "D1@Skizze1@Teil.Part"}]})
        self.assertFalse(answer["ok"])
        self.assertIn("Could not activate sheet 'Sheet1' again", answer["message"])

    def test_an_error_on_the_other_sheet_is_raised_even_if_going_back_fails(self) -> None:
        self.second_sheet()
        aufrufe = []

        def aktivieren(name):
            aufrufe.append(name)
            self.doc.current = name
            return len(aufrufe) == 1   # hin gelingt, zurück nicht

        def kaputt():
            raise OSError("HideDimension")
        self.doc.ActivateSheet, self.doc.HideDimension = aktivieren, kaputt
        with self.assertRaisesRegex(OSError, "HideDimension"):
            sw_drawing.hide_drawing_dimensions({"dimensions": [{"view": "Drawing View5",
                                                                "full_name": "D1@Skizze1@Teil.Part"}]})
        self.assertEqual(aufrufe, ["Sheet2", "Sheet1"])
        self.assertEqual(self.doc.selection, [])

    def test_arrange_reports_what_moved(self) -> None:
        answer = sw_drawing.arrange_drawing_dimensions({"view_name": "Section View A-A"})
        self.assertEqual(answer["data"]["moved"], 2)
        self.doc.extension.align_moves = False   # SOLIDWORKS answers True and moves nothing
        answer = sw_drawing.arrange_drawing_dimensions({"view_name": "Section View A-A"})
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["data"]["moved"], 0)

    def test_a_name_that_is_not_there_fails_and_names_it(self) -> None:
        answer = sw_drawing.hide_drawing_dimensions({"dimensions": [
            {"view": "Drawing View1", "full_name": "D9@Skizze1@Teil.Part"},
            {"view": "Drawing View1", "full_name": "D1@Skizze1@Teil.Part"}]})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["missing"], ["Drawing View1: D9@Skizze1@Teil.Part"])

    def test_a_dimension_that_stays_visible_fails(self) -> None:
        self.doc.HideDimension = lambda: None   # SOLIDWORKS took the call and hid nothing
        answer = sw_drawing.hide_drawing_dimensions({"dimensions": [
            {"view": "Drawing View1", "full_name": "D1@Skizze1@Teil.Part"}]})
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["remaining"], ["Drawing View1: D1@Skizze1@Teil.Part"])


REQUIRE_DRAWING = sw_drawing.require_drawing


class BindingTests(Base):
    """`document` binds a call to one drawing, checked in the same call; scale and display mode count when read back."""

    def bound(self, title: str = "Teil - Blatt1"):
        self.doc.GetTitle = lambda: title
        self.doc.GetType = lambda: 3
        return (unittest.mock.patch.object(sw_drawing, "require_drawing", REQUIRE_DRAWING),
                unittest.mock.patch.object(sw_drawing, "active_document", return_value=(None, self.doc)))

    def test_another_active_document_changes_nothing(self) -> None:
        self.doc.next_section = View("Section View A-A", kind=2, hatches=2)
        sw_drawing.insert_section_view({"x1_mm": 200, "y1_mm": 90, "x2_mm": 200, "y2_mm": 210, "place_x_mm": 80,
                                        "place_y_mm": 150, "parent_view": "Drawing View1"})
        first, second = self.bound("Fremd - Blatt1")
        with first, second:
            for call in (lambda a: sw_drawing.delete_drawing_view(dict(a, name="Section View A-A")),
                         lambda a: sw_drawing.hide_drawing_dimensions(dict(a, dimensions=[{"view": "x", "full_name": "y"}])),
                         lambda a: sw_drawing.arrange_drawing_dimensions(dict(a, view_name="Drawing View1")),
                         lambda a: sw_drawing.list_drawing_views(a)):
                answer = call({"document": "Teil - Blatt1"})
                self.assertFalse(answer["ok"])
                self.assertEqual(answer["data"]["active_document"], "Fremd - Blatt1")
        self.assertIn("Section View A-A", [v.name for v in self.doc.views])
        self.assertEqual(self.doc.aligned, [])

    def test_the_named_document_goes_ahead(self) -> None:
        first, second = self.bound()
        with first, second:
            self.assertTrue(sw_drawing.list_drawing_views({"document": "Teil - Blatt1"})["ok"])
            self.assertTrue(sw_drawing.list_drawing_views({})["ok"])

    def test_every_drawing_tool_refuses_another_document_before_acting(self) -> None:
        first, second = self.bound("Fremd - Blatt1")
        handlers = {name: fn for name, fn in sw_core.HANDLERS.items()
                    if fn.__module__ == sw_drawing.__name__ and name != "create_drawing"}
        with first, second:
            for name, fn in sorted(handlers.items()):
                with self.subTest(tool=name):
                    answer = fn({"document": "Teil - Blatt1", "name": "Drawing View1", "view_name": "Drawing View1",
                                 "x_mm": 1, "y_mm": 1, "text": "x", "place_x_mm": 1, "place_y_mm": 1,
                                 "dimensions": [{"view": "Drawing View1", "full_name": "D"}]})
                    self.assertFalse(answer["ok"])
                    self.assertEqual(answer["data"]["active_document"], "Fremd - Blatt1")
        self.assertEqual((self.doc.sketch.lines, self.doc.section_calls, self.doc.aligned), ([], [], []))
        self.assertEqual([v.name for v in self.doc.views], ["Sheet1", "Drawing View1"])

    def test_every_drawing_tool_takes_document(self) -> None:
        names = {t.name for t in sw_core.TOOLS if "document" in t.inputSchema["properties"]}
        drawing = {name for name, fn in sw_core.HANDLERS.items() if fn.__module__ == sw_drawing.__name__}
        self.assertEqual(drawing - names, {"create_drawing"})

    def test_a_scale_that_does_not_read_back_fails(self) -> None:
        class Stubborn(View):
            @property
            def ScaleDecimal(self):  # noqa: N802 - COM member name
                return 1.0

            @ScaleDecimal.setter
            def ScaleDecimal(self, value):  # noqa: N802 - COM member name
                pass
        placed = []
        self.doc.CreateDrawViewFromModelView3 = lambda *a: placed.append(Stubborn("Drawing View2")) or placed[-1]
        with unittest.mock.patch.object(sw_drawing, "_open_model_path", return_value="C:/m/teil.sldprt"), \
                unittest.mock.patch.object(sw_drawing, "_model_view_name", return_value="*Front"):
            answer = sw_drawing.insert_model_view({"x_mm": 100, "y_mm": 100, "scale": 0.5})
            self.assertFalse(answer["ok"])
            self.assertEqual(answer["data"]["scale"], {"view": "Drawing View2", "requested": 0.5, "actual": 1.0,
                                                       "ok": False})
            self.assertIn("did not apply scale 0.5", answer["message"])
            answer = sw_drawing.insert_model_view({"x_mm": 100, "y_mm": 100, "scale": 1.0})
            self.assertTrue(answer["ok"], answer)
            self.assertTrue(answer["data"]["scale"]["ok"])


class AxisPointTests(unittest.TestCase):
    def test_cylinder_reports_a_point_on_its_axis(self) -> None:
        class Surface:
            Identity = 4002
            CylinderParams = (0.01, 0.02, 0.03, 1.0, 0.0, 0.0, 0.005)

            def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
                pass

        details = sw_core._surface_details(Surface())
        self.assertEqual(details["axis_point_mm"], [10.0, 20.0, 30.0])
        self.assertEqual(details["radius_mm"], 5.0)

    def test_a_bore_is_concave_and_a_shaft_is_not(self) -> None:
        class Surface:
            Identity = 4002
            CylinderParams = (0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.004)

            def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
                pass

        class Face:
            def __init__(self, sense) -> None:
                self.FaceInSurfaceSense = sense
                self.GetArea = 1e-4

            def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
                pass

            def GetSurface(self):  # noqa: N802 - COM member name
                return Surface()

        class Body:
            def GetFaces(self):  # noqa: N802 - COM member name
                return (Face(True), Face(False), Face(None))

        with unittest.mock.patch.object(sw_core, "iter_body_context", return_value=[(Body(), "Body", None)]):
            faces = sw_core.enumerate_faces(object())
        self.assertEqual([f["concave"] for f in faces], [True, False, None])


if __name__ == "__main__":
    unittest.main()
