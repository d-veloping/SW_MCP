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

"""Offline tests for set_sheet_format, set_dimension_style, move_dimension_text, list_view_arcs and the reference
points of list_drawing_dimensions.

The fakes follow what SOLIDWORKS 2016 SP3 did in the ClauSW spike zeichner-ansichtswahl (2026-10-30): SetupSheet5
with a custom template puts the sheet format on the sheet and GetTemplateName reads its path back, IDisplayDimension
carries ShortenedRadius and SolidLeader as properties, IAnnotation.SetPosition takes and GetPosition returns sheet
metres, GetVisibleEntities2 lists the drawn edges, and model points reach the sheet through ModelToViewTransform.
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_drawing

REQUIRE_DRAWING = sw_drawing.require_drawing


class Methods:
    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass


# ---------------------------------------------------------------------------- sheet format


class Sheet(Methods):
    def __init__(self, name: str = "Blatt1") -> None:
        self.name = name
        self.template = r"C:\formats\old.slddrt"
        self.properties = [6, 12, 1.0, 2.0, False, 0.297, 0.21]   # A4, scale 1:2, third angle

    def GetName(self) -> str:  # noqa: N802 - COM member name
        return self.name

    def GetProperties2(self):  # noqa: N802 - COM member name
        return tuple(self.properties)

    def GetTemplateName(self) -> str:  # noqa: N802 - COM member name
        return self.template


class FormatDrawing(Methods):
    def __init__(self) -> None:
        self.sheet = Sheet()
        self.calls: list[tuple] = []
        self.answer: object = True
        self.applies = True
        self.after: dict = {}     # overrides for what the sheet reads back after SetupSheet5

    def GetTitle(self) -> str:  # noqa: N802 - COM member name
        return "Teil - Blatt1"

    def GetType(self) -> int:  # noqa: N802 - COM member name
        return 3

    def GetCurrentSheet(self) -> Sheet:  # noqa: N802 - COM member name
        return self.sheet

    def Sheet(self, name: str):  # noqa: N802 - COM member name
        return self.sheet if name == self.sheet.name else None

    def SetupSheet5(self, name, size, template, scale1, scale2, first, path, w, h, view, remove):  # noqa: N802
        self.calls.append((name, size, template, scale1, scale2, first, path, round(w, 6), round(h, 6), view, remove))
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.applies:
            self.sheet.template = self.after.get("template", path)
            self.sheet.properties = [size, template, self.after.get("scale1", scale1), scale2,
                                     self.after.get("first", first), self.after.get("w", w), self.after.get("h", h)]
        return self.answer


class SetSheetFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = FormatDrawing()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.template = Path(folder.name) / "DEV_2026.slddrt"
        self.template.write_bytes(b"format")
        for patch in (unittest.mock.patch.object(sw_drawing, "require_drawing", return_value=(None, self.doc)),
                      unittest.mock.patch.object(sw_drawing, "rebuild")):
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, **args):
        return sw_drawing.set_sheet_format({"template_path": str(self.template), **args})

    def test_the_format_is_set_with_the_sheet_scale_and_read_back(self) -> None:
        answer = self.call()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.doc.calls, [("Blatt1", 8, 12, 1.0, 2.0, True, str(self.template), 0.42, 0.297, "",
                                           True)])
        data = answer["data"]
        self.assertEqual((data["template"], data["sheet_size_mm"], data["first_angle"], data["scale"]),
                         (str(self.template), [420.0, 297.0], True, [1.0, 2.0]))

    def test_paper_size_projection_and_named_sheet(self) -> None:
        answer = self.call(paper_size="A2", first_angle=False, sheet="Blatt1")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.doc.calls[0][1], 9)
        self.assertIs(self.doc.calls[0][5], False)

    def test_a_missing_or_wrong_file_calls_nothing(self) -> None:
        other = self.template.with_suffix(".drwdot")
        other.write_bytes(b"x")
        for path in (str(self.template.with_name("missing.slddrt")), str(other), "DEV_2026.slddrt", ""):
            with self.subTest(path=path):
                answer = sw_drawing.set_sheet_format({"template_path": path})
                self.assertFalse(answer["ok"])
                self.assertIn("nothing was changed", answer["message"])
        self.assertEqual(self.doc.calls, [])

    def test_an_unknown_sheet_or_paper_size_calls_nothing(self) -> None:
        self.assertFalse(self.call(sheet="Blatt9")["ok"])
        self.assertFalse(self.call(paper_size="custom")["ok"])
        self.assertEqual(self.doc.calls, [])

    def test_setup_returning_false_fails_with_the_template_read(self) -> None:
        self.doc.answer, self.doc.applies = False, False
        answer = self.call()
        self.assertFalse(answer["ok"])
        self.assertIn("SetupSheet5 returned false", answer["message"])
        self.assertEqual(answer["data"]["template"], r"C:\formats\old.slddrt")
        self.assertIs(answer["data"]["setup_returned"], False)

    def test_setup_returning_false_fails_even_when_everything_reads_back(self) -> None:
        self.doc.answer = False
        self.assertFalse(self.call()["ok"])

    def test_a_com_exception_in_setup_fails(self) -> None:
        self.doc.answer = OSError("RPC_E_SERVERFAULT")
        answer = self.call()
        self.assertFalse(answer["ok"])
        self.assertIn("SetupSheet5 raised", answer["message"])

    def test_every_readback_that_differs_fails(self) -> None:
        cases = {"template": {"template": r"C:\formats\old.slddrt"}, "size": {"w": 0.297, "h": 0.21},
                 "projection": {"first": False}, "scale": {"scale1": 2.0}}
        words = {"template": "uses", "size": "not 420 x 297", "projection": "third angle", "scale": "scale changed"}
        for name, after in cases.items():
            with self.subTest(readback=name):
                self.doc.sheet, self.doc.after = Sheet(), after
                answer = self.call()
                self.assertFalse(answer["ok"])
                self.assertIn(words[name], answer["message"])

    def test_the_template_counts_by_its_path_not_its_case(self) -> None:
        self.doc.after = {"template": str(self.template).upper()}
        self.assertTrue(self.call()["ok"])
        self.doc.sheet, self.doc.after = Sheet(), {"template": r"D:\elsewhere\DEV_2026.slddrt"}
        self.assertFalse(self.call()["ok"])

    def test_unreadable_sheet_properties_call_nothing(self) -> None:
        self.doc.sheet.properties = [6, 12, 1.0]
        answer = self.call()
        self.assertFalse(answer["ok"])
        self.assertIs(answer["data"]["readable"], False)
        self.assertEqual(self.doc.calls, [])

    def test_another_active_document_changes_nothing(self) -> None:
        with unittest.mock.patch.object(sw_drawing, "require_drawing", REQUIRE_DRAWING), \
                unittest.mock.patch.object(sw_drawing, "active_document", return_value=(None, self.doc)):
            answer = self.call(document="Andere - Blatt1")
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["active_document"], "Teil - Blatt1")
        self.assertEqual(self.doc.calls, [])


# ---------------------------------------------------------------------------- dimensions


class Point:
    def __init__(self, *xyz: float) -> None:
        self.ArrayData = xyz


class Dimension:
    def __init__(self, name: str, points=None) -> None:
        self.FullName, self.SystemValue, self.points = name, 0.02, points

    def GetType(self) -> int:  # noqa: N802 - COM member name
        return 0

    @property
    def ReferencePoints(self):  # noqa: N802 - COM member name
        if isinstance(self.points, Exception):
            raise self.points
        return None if self.points is None else tuple(Point(*p) for p in self.points)


class Annotation:
    Visible, Layer = 1, ""

    def __init__(self) -> None:
        self.position = (0.1, 0.1, 0.0)
        self.snap: tuple[float, float] | None = None   # where SOLIDWORKS puts the text instead
        self.fails: Exception | None = None
        self.unreadable = False

    def SetPosition(self, x, y, z) -> bool:  # noqa: N802 - COM member name
        if self.fails:
            raise self.fails
        self.position = (*(self.snap or (x, y)), z)
        return True

    def GetPosition(self):  # noqa: N802 - COM member name
        if self.unreadable:
            raise OSError("no position")
        return self.position


class Display(Methods):
    Type2, ShowParenthesis = 5, False

    def __init__(self, dimension: Dimension, view: "View") -> None:
        self.dimension, self.view, self.annotation = dimension, view, Annotation()
        self.ShortenedRadius, self.SolidLeader = True, True

    def GetDimension2(self, index: int) -> Dimension:  # noqa: N802 - COM member name
        return self.dimension

    def GetAnnotation(self) -> Annotation:  # noqa: N802 - COM member name
        return self.annotation

    def GetNameForSelection(self) -> str:  # noqa: N802 - COM member name
        return self.dimension.FullName

    def GetNext5(self):  # noqa: N802 - COM member name
        i = self.view.displays.index(self)
        return self.view.displays[i + 1] if i + 1 < len(self.view.displays) else None


class Unknown(Display):
    """A build that does not know SolidLeader: setting and reading it raise, as late-bound pywin32 does."""

    def __getattribute__(self, name: str):
        if name == "SolidLeader":
            raise AttributeError("SldWorks.DisplayDimension.SolidLeader")
        return super().__getattribute__(name)

    def __init__(self, dimension: Dimension, view: "View") -> None:
        super().__init__(dimension, view)
        self.__dict__["ready"] = True

    def __setattr__(self, name: str, value) -> None:
        if name == "SolidLeader" and self.__dict__.get("ready"):
            raise AttributeError("Property 'SldWorks.DisplayDimension.SolidLeader' can not be set.")
        super().__setattr__(name, value)


class Stubborn(Display):
    """SOLIDWORKS takes ShortenedRadius without an error and keeps the old value."""

    def __setattr__(self, name: str, value) -> None:
        if name == "ShortenedRadius" and hasattr(self, "ShortenedRadius"):
            return
        super().__setattr__(name, value)


class Coupled(Display):
    """Setting ShortenedRadius also flips SolidLeader."""

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name == "ShortenedRadius" and hasattr(self, "SolidLeader"):
            super().__setattr__("SolidLeader", not self.SolidLeader)


class Transform:
    # front view, scale 2, model origin at (200, 150) mm on the sheet
    ArrayData = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0.2, 0.15, 0.0, 2.0, 0, 0, 0)


class View(Methods):
    def __init__(self, name: str, kind: int = 7) -> None:
        self.name, self.Type = name, kind
        self.next = None
        self.displays: list[Display] = []
        self.edges: list = []
        self.ModelToViewTransform = Transform()

    def GetName2(self) -> str:  # noqa: N802 - COM member name
        return self.name

    def GetNextView(self):  # noqa: N802 - COM member name
        return self.next

    def GetFirstDisplayDimension5(self):  # noqa: N802 - COM member name
        return self.displays[0] if self.displays else None

    def GetVisibleEntities2(self, component, kind):  # noqa: N802 - COM member name
        assert kind == 1, "only edges are ever read, never silhouettes"
        return tuple(self.edges)


class Drawing(Methods):
    def __init__(self, views: list[View]) -> None:
        self.views = views

    def GetTitle(self) -> str:  # noqa: N802 - COM member name
        return "Teil - Blatt1"

    def GetType(self) -> int:  # noqa: N802 - COM member name
        return 3

    def GetFirstView(self):  # noqa: N802 - COM member name
        for a, b in zip(self.views, self.views[1:] + [None]):
            a.next = b
        return self.views[0]

    def GetViews(self):  # noqa: N802 - COM member name
        return (tuple(self.views),)

    def Sheet(self, name: str):  # noqa: N802 - COM member name
        return Sheet(name)


class DimensionToolsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.view = View("Drawing View1")
        self.doc = Drawing([View("Blatt1", kind=1), self.view])
        self.radius = Display(Dimension("D1@Fillet1@Teil.Part"), self.view)
        self.width = Display(Dimension("D1@Skizze1@Teil.Part"), self.view)
        self.view.displays = [self.radius, self.width]
        for patch in (unittest.mock.patch.object(sw_drawing, "require_drawing", return_value=(None, self.doc)),
                      unittest.mock.patch.object(sw_drawing, "rebuild"),
                      unittest.mock.patch.object(sw_drawing, "nothing", return_value=None),
                      unittest.mock.patch.object(sw_drawing, "double_array", side_effect=lambda v: tuple(v))):
            patch.start()
            self.addCleanup(patch.stop)

    def replace(self, old: Display, cls: type) -> Display:
        new = cls(old.dimension, self.view)
        self.view.displays[self.view.displays.index(old)] = new
        return new

    def wrong_document(self, call) -> dict:
        with unittest.mock.patch.object(sw_drawing, "require_drawing", REQUIRE_DRAWING), \
                unittest.mock.patch.object(sw_drawing, "active_document", return_value=(None, self.doc)):
            return call({"document": "Andere - Blatt1", "view_name": "Drawing View1"})


class SetDimensionStyleTests(DimensionToolsBase):
    def style(self, names=("D1@Fillet1@Teil.Part",), **args):
        return sw_drawing.set_dimension_style({"view_name": "Drawing View1", "dimensions": list(names), **args})

    def test_only_the_requested_property_is_set_and_both_are_read_back(self) -> None:
        answer = self.style(shortened_radius=False)
        self.assertTrue(answer["ok"], answer)
        entry = answer["data"]["dimensions"][0]
        self.assertEqual((entry["shortened_radius"], entry["solid_leader"], entry["ok"]), (False, True, True))
        self.assertEqual((self.radius.ShortenedRadius, self.radius.SolidLeader), (False, True))
        self.assertEqual((self.width.ShortenedRadius, self.width.SolidLeader), (True, True))

    def test_both_properties(self) -> None:
        answer = self.style(shortened_radius=False, solid_leader=False)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((self.radius.ShortenedRadius, self.radius.SolidLeader), (False, False))

    def test_a_property_the_build_does_not_know_is_an_error(self) -> None:
        display = self.replace(self.radius, Unknown)
        answer = self.style(shortened_radius=False, solid_leader=False)
        self.assertFalse(answer["ok"])
        entry = answer["data"]["dimensions"][0]
        self.assertIn("SolidLeader", entry["error"])
        self.assertIsNone(entry["solid_leader"])
        self.assertIs(display.ShortenedRadius, False)

    def test_a_value_that_does_not_read_back_fails(self) -> None:
        self.replace(self.radius, Stubborn)
        answer = self.style(shortened_radius=False)
        self.assertFalse(answer["ok"])
        self.assertIn("reads back as shortened_radius=True", answer["data"]["dimensions"][0]["error"])

    def test_an_unrequested_property_that_changes_fails(self) -> None:
        self.replace(self.radius, Coupled)
        answer = self.style(shortened_radius=False)
        self.assertFalse(answer["ok"])
        self.assertIn("changed without being asked: solid_leader", answer["data"]["dimensions"][0]["error"])

    def test_a_missing_dimension_fails_and_the_others_are_set(self) -> None:
        answer = self.style(names=("D9@Nichts@Teil.Part", "D1@Fillet1@Teil.Part"), shortened_radius=False)
        self.assertFalse(answer["ok"])
        missing, found = answer["data"]["dimensions"]
        self.assertEqual((missing["found"], missing["ok"]), (False, False))
        self.assertIn("not in view", missing["error"])
        self.assertTrue(found["ok"])

    def test_a_dimension_shown_twice_is_not_touched(self) -> None:
        twin = Display(self.radius.dimension, self.view)
        self.view.displays.append(twin)
        answer = self.style(shortened_radius=False)
        self.assertFalse(answer["ok"])
        self.assertIn("shown 2 times", answer["data"]["dimensions"][0]["error"])
        self.assertEqual((self.radius.ShortenedRadius, twin.ShortenedRadius), (True, True))

    def test_no_property_or_no_dimension_changes_nothing(self) -> None:
        for answer in (self.style(), self.style(names=(), solid_leader=False),
                       self.style(names=("D1@Fillet1@Teil.Part", "D1@Fillet1@Teil.Part"), solid_leader=False),
                       self.style(solid_leader="no")):
            self.assertFalse(answer["ok"])
            self.assertIn("nothing was changed", answer["message"])
        self.assertEqual((self.radius.ShortenedRadius, self.radius.SolidLeader), (True, True))

    def test_an_unknown_view_changes_nothing(self) -> None:
        answer = sw_drawing.set_dimension_style({"view_name": "Drawing View9", "dimensions": ["D1@Fillet1@Teil.Part"],
                                                 "shortened_radius": False})
        self.assertFalse(answer["ok"])
        self.assertIs(self.radius.ShortenedRadius, True)

    def test_a_walk_refused_after_the_rebuild_fails(self) -> None:
        original = self.view.GetFirstDisplayDimension5
        calls = []

        def walk():
            calls.append(1)
            if len(calls) > 1:
                raise OSError("walk refused")
            return original()
        self.view.GetFirstDisplayDimension5 = walk
        answer = self.style(shortened_radius=False)
        self.assertFalse(answer["ok"])
        self.assertIs(answer["data"]["readable"], False)

    def test_another_active_document_changes_nothing(self) -> None:
        answer = self.wrong_document(lambda a: sw_drawing.set_dimension_style(
            dict(a, dimensions=["D1@Fillet1@Teil.Part"], shortened_radius=False)))
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["active_document"], "Teil - Blatt1")
        self.assertIs(self.radius.ShortenedRadius, True)


class MoveDimensionTextTests(DimensionToolsBase):
    def move(self, *moves):
        return sw_drawing.move_dimension_text({"view_name": "Drawing View1", "moves": list(moves)})

    def test_a_text_is_moved_and_read_back(self) -> None:
        answer = self.move({"full_name": "D1@Fillet1@Teil.Part", "x_mm": 250.5, "y_mm": 180})
        self.assertTrue(answer["ok"], answer)
        entry = answer["data"]["moves"][0]
        self.assertEqual((entry["before_mm"], entry["read_mm"], entry["deviation_mm"]),
                         ([100.0, 100.0], [250.5, 180.0], 0.0))
        self.assertEqual(self.width.annotation.position, (0.1, 0.1, 0.0))

    def test_a_text_that_lands_elsewhere_fails(self) -> None:
        self.radius.annotation.snap = (0.2504, 0.18)   # 0.1 mm off: within tolerance
        self.width.annotation.snap = (0.2, 0.1)       # 10 mm off
        answer = self.move({"full_name": "D1@Fillet1@Teil.Part", "x_mm": 250.5, "y_mm": 180},
                           {"full_name": "D1@Skizze1@Teil.Part", "x_mm": 210, "y_mm": 100})
        self.assertFalse(answer["ok"])
        near, far = answer["data"]["moves"]
        self.assertTrue(near["ok"])
        self.assertEqual((far["ok"], far["deviation_mm"]), (False, 10.0))

    def test_a_com_exception_in_set_position_fails(self) -> None:
        self.radius.annotation.fails = OSError("RPC_E_SERVERFAULT")
        answer = self.move({"full_name": "D1@Fillet1@Teil.Part", "x_mm": 250, "y_mm": 180})
        self.assertFalse(answer["ok"])
        self.assertIn("SetPosition raised", answer["data"]["moves"][0]["error"])

    def test_an_unreadable_position_fails(self) -> None:
        self.radius.annotation.unreadable = True
        answer = self.move({"full_name": "D1@Fillet1@Teil.Part", "x_mm": 250, "y_mm": 180})
        self.assertFalse(answer["ok"])
        entry = answer["data"]["moves"][0]
        self.assertEqual((entry["read_mm"], entry["error"]), (None, "position not readable"))

    def test_a_missing_or_doubled_dimension_is_not_moved(self) -> None:
        self.view.displays.append(Display(self.width.dimension, self.view))
        answer = self.move({"full_name": "D9@Nichts@Teil.Part", "x_mm": 1, "y_mm": 1},
                           {"full_name": "D1@Skizze1@Teil.Part", "x_mm": 1, "y_mm": 1},
                           {"full_name": "D1@Fillet1@Teil.Part", "x_mm": 250, "y_mm": 180})
        self.assertFalse(answer["ok"])
        missing, doubled, moved = answer["data"]["moves"]
        self.assertIn("not in view", missing["error"])
        self.assertIn("shown 2 times", doubled["error"])
        self.assertTrue(moved["ok"])
        self.assertEqual(self.width.annotation.position, (0.1, 0.1, 0.0))

    def test_invalid_moves_change_nothing(self) -> None:
        for moves in ([], [{"full_name": "D1@Fillet1@Teil.Part", "x_mm": math.nan, "y_mm": 1}],
                      [{"full_name": "D1@Fillet1@Teil.Part", "x_mm": 1}], [{"full_name": "", "x_mm": 1, "y_mm": 1}],
                      [{"full_name": "D1@Fillet1@Teil.Part", "x_mm": 1, "y_mm": 1}] * 2):
            with self.subTest(moves=moves):
                answer = self.move(*moves)
                self.assertFalse(answer["ok"])
                self.assertIn("nothing was changed", answer["message"])
        self.assertEqual(self.radius.annotation.position, (0.1, 0.1, 0.0))

    def test_another_active_document_changes_nothing(self) -> None:
        answer = self.wrong_document(lambda a: sw_drawing.move_dimension_text(
            dict(a, moves=[{"full_name": "D1@Fillet1@Teil.Part", "x_mm": 1, "y_mm": 1}])))
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["active_document"], "Teil - Blatt1")
        self.assertEqual(self.radius.annotation.position, (0.1, 0.1, 0.0))


class ReferencePointTests(DimensionToolsBase):
    def points(self) -> dict:
        answer = sw_drawing.list_drawing_dimensions({"view_name": "Drawing View1"})
        self.assertTrue(answer["ok"], answer)
        return {d["full_name"]: d["reference_points_mm"] for d in answer["data"]["dimensions"]}

    def test_points_reach_the_sheet_through_the_view_transform(self) -> None:
        self.radius.dimension.points = [(0.01, 0.005, 0.0), (0.02, 0.005, 0.003)]
        self.assertEqual(self.points()["D1@Fillet1@Teil.Part"], [[220.0, 160.0], [240.0, 160.0]])

    def test_unreadable_points_are_null_never_empty(self) -> None:
        for points in (None, [], OSError("no points"), [(0.01, math.inf, 0.0)], [(0.01, 0.0)]):
            with self.subTest(points=points):
                self.radius.dimension.points = points
                self.assertIsNone(self.points()["D1@Fillet1@Teil.Part"])

    def test_an_unreadable_transform_gives_null_points(self) -> None:
        self.radius.dimension.points = [(0.01, 0.005, 0.0)]
        self.view.ModelToViewTransform = None
        self.assertIsNone(self.points()["D1@Fillet1@Teil.Part"])


# ---------------------------------------------------------------------------- arcs


class Curve(Methods):
    """A circle about `centre` with `axis`, from angle a0 to a1 (degrees, counter-clockwise in the xy plane for axis
    z), or a straight line when radius is None."""

    def __init__(self, centre=(0.0, 0.0, 0.0), radius=None, a0=0.0, a1=360.0, axis=(0.0, 0.0, 1.0),
                 start=(0.0, 0.0, 0.0), end=(0.01, 0.0, 0.0)) -> None:
        self.centre, self.radius, self.a0, self.a1, self.axis = centre, radius, a0, a1, axis
        self.start, self.end = (self.at(a0), self.at(a1)) if radius else (start, end)
        self.broken = ""

    def at(self, degrees: float) -> tuple[float, float, float]:
        a = math.radians(degrees)
        return (self.centre[0] + self.radius * math.cos(a), self.centre[1] + self.radius * math.sin(a),
                self.centre[2])

    def IsCircle(self) -> bool:  # noqa: N802 - COM member name
        return self.radius is not None

    def CircleParams(self):  # noqa: N802 - COM member name
        if self.broken == "circle":
            raise OSError("server fault")
        return (*self.centre, *self.axis, self.radius)

    def GetTessPts(self, chord, length, start, end):  # noqa: N802 - COM member name
        if self.broken == "tess":
            return ()
        steps = 8
        return tuple(c for i in range(steps + 1) for c in self.at(self.a0 + (self.a1 - self.a0) * i / steps))


class Edge:
    def __init__(self, curve: Curve) -> None:
        self.curve = curve

    def GetCurveParams2(self):  # noqa: N802 - COM member name
        return (*self.curve.start, *self.curve.end, 0.0, 1.0)

    def GetCurve(self) -> Curve:  # noqa: N802 - COM member name
        if self.curve.broken == "curve":
            raise OSError("server fault")
        return self.curve


class ListViewArcsTests(DimensionToolsBase):
    def setUp(self) -> None:
        super().setUp()
        self.hole = Edge(Curve(centre=(0.01, 0.01, 0.0), radius=0.004))
        self.fillet = Edge(Curve(centre=(0.03, 0.01, 0.0), radius=0.005, a0=0.0, a1=90.0))
        self.side = Edge(Curve())
        self.across = Edge(Curve(centre=(0.0, 0.0, 0.0), radius=0.004, axis=(1.0, 0.0, 0.0)))
        self.view.edges = [self.hole, self.fillet, self.side, self.across]

    def arcs(self) -> dict:
        return sw_drawing.list_view_arcs({"view_name": "Drawing View1"})

    def test_true_shape_circles_and_arcs_on_the_sheet(self) -> None:
        answer = self.arcs()
        self.assertTrue(answer["ok"], answer)
        data = answer["data"]
        self.assertEqual((data["edges"], data["oblique"], data["unreadable"], len(data["arcs"])), (4, 1, 0, 2))
        hole, fillet = data["arcs"]
        self.assertEqual((hole["centre_mm"], hole["radius_mm"], hole["full_circle"]), ([220.0, 170.0], 8.0, True))
        self.assertEqual((fillet["centre_mm"], fillet["radius_mm"], fillet["full_circle"]), ([260.0, 170.0], 10.0, False))
        # half way along a quarter arc from 0 to 90 degrees: at 45 degrees
        half = 10.0 / math.sqrt(2)
        self.assertAlmostEqual(fillet["mid_mm"][0], 260.0 + half, delta=0.01)
        self.assertAlmostEqual(fillet["mid_mm"][1], 170.0 + half, delta=0.01)
        self.assertEqual((fillet["points_mm"][0], fillet["points_mm"][-1]), ([270.0, 170.0], [260.0, 180.0]))

    def test_an_unreadable_edge_is_counted_never_left_out(self) -> None:
        for broken in ("curve", "circle", "tess"):
            with self.subTest(broken=broken):
                self.fillet.curve.broken = broken
                answer = self.arcs()
                self.assertTrue(answer["ok"], answer)
                self.assertEqual((answer["data"]["unreadable"], len(answer["data"]["arcs"])), (1, 1))
                self.assertIn("1 edges not readable", answer["message"])

    def test_a_view_without_circular_edges_lists_none(self) -> None:
        self.view.edges = [self.side, self.across]
        answer = self.arcs()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["data"]["arcs"], answer["data"]["unreadable"], answer["data"]["oblique"]),
                         ([], 0, 1))

    def test_a_view_that_lists_no_edge_fails(self) -> None:
        self.view.edges = []
        answer = self.arcs()
        self.assertFalse(answer["ok"])
        self.assertIs(answer["data"]["readable"], False)

    def test_an_unreadable_transform_fails(self) -> None:
        self.view.ModelToViewTransform = None
        answer = self.arcs()
        self.assertFalse(answer["ok"])
        self.assertIn("model transform", answer["message"])

    def test_an_unknown_view_fails(self) -> None:
        self.assertFalse(sw_drawing.list_view_arcs({"view_name": "Drawing View9"})["ok"])

    def test_another_active_document_is_refused(self) -> None:
        answer = self.wrong_document(sw_drawing.list_view_arcs)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["active_document"], "Teil - Blatt1")


if __name__ == "__main__":
    unittest.main()
