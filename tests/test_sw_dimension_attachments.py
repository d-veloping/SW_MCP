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

"""Offline tests for check_dimension_attachments.

The fakes follow what SOLIDWORKS 2016 SP3 did in the ClauSW spike zeichner-ansichtswahl (2026-10-30):
IView.GetVisibleEntities2 lists the edges the view draws with hidden lines removed (and keeps doing so after a switch
to hidden lines visible), the part's solid bodies list every edge, IDimension.ReferencePoints are model points in
metres, and the view maps them onto the sheet through ModelToViewTransform.
"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_drawing


class Point:
    def __init__(self, *xyz: float) -> None:
        self.ArrayData = xyz


class Curve:
    def __init__(self, start, end, centre=None) -> None:
        self.start, self.end, self.centre = start, end, centre

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def IsCircle(self) -> bool:  # noqa: N802 - COM member name
        return self.centre is not None

    def CircleParams(self):  # noqa: N802 - COM member name
        return (*self.centre, 0.0, 0.0, 1.0, 0.002)

    def GetTessPts(self, chord, length, start, end):  # noqa: N802 - COM member name
        if self.centre is None:
            return (*self.start, *self.end)
        # a full circle of radius 2 mm as a square of four chords is enough for the distance test
        cx, cy, cz = self.centre
        corners = [(cx + 0.002, cy, cz), (cx, cy + 0.002, cz), (cx - 0.002, cy, cz), (cx, cy - 0.002, cz),
                   (cx + 0.002, cy, cz)]
        return tuple(c for p in corners for c in p)


class Edge:
    def __init__(self, curve: Curve, broken: bool = False) -> None:
        self.curve, self.broken = curve, broken

    def GetCurveParams2(self):  # noqa: N802 - COM member name
        if self.broken:
            raise OSError("server fault")
        return (*self.curve.start, *self.curve.end, 0.0, 1.0)

    def GetCurve(self) -> Curve:  # noqa: N802 - COM member name
        return self.curve


class Dimension:
    def __init__(self, name: str, points=None) -> None:
        self.FullName, self.SystemValue, self.points = name, 0.015, points

    def GetType(self) -> int:  # noqa: N802 - COM member name
        return 0

    @property
    def ReferencePoints(self):  # noqa: N802 - COM member name
        if self.points is None:
            raise OSError("no points")
        return tuple(Point(*p) for p in self.points)


class Annotation:
    Visible, Layer = 1, ""

    def GetPosition(self):  # noqa: N802 - COM member name
        return (0.1, 0.1, 0.0)


class Display:
    Type2, ShowParenthesis = 12, False

    def __init__(self, dimension: Dimension, view: "View") -> None:
        self.dimension, self.view, self.annotation = dimension, view, Annotation()

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def GetDimension2(self, index: int) -> Dimension:  # noqa: N802 - COM member name
        return self.dimension

    def GetAnnotation(self) -> Annotation:  # noqa: N802 - COM member name
        return self.annotation

    def GetNameForSelection(self) -> str:  # noqa: N802 - COM member name
        return self.dimension.FullName

    def GetNext5(self):  # noqa: N802 - COM member name
        i = self.view.displays.index(self)
        return self.view.displays[i + 1] if i + 1 < len(self.view.displays) else None


class Body:
    def __init__(self, edges: list[Edge]) -> None:
        self.edges = edges

    def GetEdges(self):  # noqa: N802 - COM member name
        return tuple(self.edges)


class Model:
    def __init__(self, view: "View") -> None:
        self.view = view
        self.calls: list[tuple] = []

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def GetBodies2(self, kind, visible_only):  # noqa: N802 - COM member name
        self.calls.append((kind, visible_only))
        edges = self.view.shown_edges + self.view.hidden_edges
        return (Body(edges[:2]), Body(edges[2:])) if edges else ()


class Transform:
    # front view, scale 1, model origin at (200, 150) mm on the sheet
    ArrayData = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0.2, 0.15, 0.0, 1.0, 0, 0, 0)


class View:
    def __init__(self, name: str, kind: int = 7) -> None:
        self.name, self.Type = name, kind
        self.mode = 2
        self.refuse: set[int] = set()     # modes SetDisplayMode3 accepts and does not apply
        self.set_calls: list[int] = []
        self.next = None
        self.shown_edges: list[Edge] = []
        self.hidden_edges: list[Edge] = []
        self.displays: list[Display] = []
        self.ModelToViewTransform = Transform()
        self.ReferencedDocument = Model(self)

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def GetName2(self) -> str:  # noqa: N802 - COM member name
        return self.name

    def GetNextView(self):  # noqa: N802 - COM member name
        return self.next

    def GetDisplayMode2(self):  # noqa: N802 - COM member name
        return self.mode

    def GetDisplayEdgesInShadedMode(self) -> bool:  # noqa: N802 - COM member name
        return False

    def SetDisplayMode3(self, use_parent, mode, faceted, edges):  # noqa: N802 - COM member name
        self.set_calls.append(mode)
        if mode not in self.refuse:
            self.mode = mode
        return True

    def GetVisibleEntities2(self, component, kind):  # noqa: N802 - COM member name
        assert kind == 1, "only edges are ever read"
        return tuple(self.shown_edges)

    def GetFirstDisplayDimension5(self):  # noqa: N802 - COM member name
        return self.displays[0] if self.displays else None


class Drawing:
    def __init__(self, views: list[View]) -> None:
        self.views = views

    def GetFirstView(self):  # noqa: N802 - COM member name
        for a, b in zip(self.views, self.views[1:] + [None]):
            a.next = b
        return self.views[0]


def line(a, b) -> Edge:
    return Edge(Curve(a, b))


class CheckDimensionAttachmentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.view = View("Drawing View1")
        self.doc = Drawing([View("Sheet1", kind=1), self.view])
        # a block 40 x 20 with a blind bore whose bottom (y = 5 mm) is hidden in this view, and a visible hole
        self.view.shown_edges = [line((0, 0, 0), (0.04, 0, 0)), line((0, 0.02, 0), (0.04, 0.02, 0)),
                                 Edge(Curve((0.032, 0.01, 0), (0.032, 0.01, 0), centre=(0.03, 0.01, 0)))]
        self.view.hidden_edges = [line((0.01, 0.005, 0), (0.02, 0.005, 0))]
        self.dims = {
            "depth": Dimension("D1@Bohrung@Teil.Part", [(0.015, 0.02, 0.0001), (0.015, 0.005, 0.0001)]),
            "width": Dimension("D1@Skizze1@Teil.Part", [(0.0, 0.02, 0.0), (0.04, 0.02, 0.0)]),
            "origin": Dimension("D2@Skizze1@Teil.Part", [(0.0, -0.01, 0.0), (-0.05, -0.01, 0.0)]),
            "radius": Dimension("D1@Loch@Teil.Part", [(0.03, 0.01, 0.0), (0.03, 0.01, 0.0)]),
            "unreadable": Dimension("D3@Skizze1@Teil.Part"),
        }
        self.view.displays = [Display(d, self.view) for d in self.dims.values()]
        for patch in (
            unittest.mock.patch.object(sw_drawing, "require_drawing", return_value=(None, self.doc)),
            unittest.mock.patch.object(sw_drawing, "rebuild"),
            unittest.mock.patch.object(sw_drawing, "nothing", return_value=None),
            unittest.mock.patch.object(sw_drawing, "double_array", side_effect=lambda v: tuple(v)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def check(self, **args):
        return sw_drawing.check_dimension_attachments({"view_name": "Drawing View1", **args})

    def by_name(self, answer) -> dict:
        return {d["full_name"]: d for d in answer["data"]["dimensions"]}

    def test_only_a_point_on_a_hidden_edge_points_into_the_void(self) -> None:
        answer = self.check()
        self.assertTrue(answer["ok"], answer)
        found = self.by_name(answer)
        depth = found["D1@Bohrung@Teil.Part"]
        self.assertIs(depth["into_void"], True)
        self.assertEqual(depth["hidden_points"], [False, True])
        self.assertEqual(depth["sheet_points_mm"], [[215.0, 170.0], [215.0, 155.0]])
        self.assertIs(found["D1@Skizze1@Teil.Part"]["into_void"], False)
        # on no edge at all (sketch origin, construction geometry): not hidden
        self.assertIs(found["D2@Skizze1@Teil.Part"]["into_void"], False)
        # a circle's centre carries a radius
        self.assertIs(found["D1@Loch@Teil.Part"]["into_void"], False)
        self.assertEqual((answer["data"]["into_void"], answer["data"]["unjudged"]), (1, 1))
        self.assertEqual(answer["data"]["edges"], {"all": 4, "shown": 3, "unreadable": 0})
        self.assertEqual(self.view.ReferencedDocument.calls, [(0, True)])

    def test_unreadable_points_give_no_verdict(self) -> None:
        unreadable = self.by_name(self.check())["D3@Skizze1@Teil.Part"]
        self.assertIsNone(unreadable["into_void"])
        self.assertNotIn("hidden_points", unreadable)

    def test_the_display_mode_is_set_back_and_read_back(self) -> None:
        self.view.mode = 3
        answer = self.check()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.view.set_calls, [2, 3])
        self.assertEqual(answer["data"]["display_mode"], "shaded")

    def test_a_mode_that_does_not_apply_fails_and_is_set_back(self) -> None:
        self.view.mode = 3
        self.view.refuse = {2}
        answer = self.check()
        self.assertFalse(answer["ok"])
        self.assertIn("hidden lines removed", answer["message"])
        self.assertEqual(self.view.mode, 3)
        self.assertEqual(self.view.set_calls, [2, 3])

    def test_a_view_without_solid_bodies_fails_and_keeps_its_mode(self) -> None:
        self.view.mode = 3
        self.view.shown_edges, self.view.hidden_edges = [], []
        answer = self.check()
        self.assertFalse(answer["ok"])
        self.assertIn("no part with solid bodies", answer["message"])
        self.assertEqual(self.view.mode, 3)
        self.view.ReferencedDocument = None
        self.assertIn("no part with solid bodies", self.check()["message"])

    def test_a_mode_that_does_not_come_back_fails(self) -> None:
        self.view.mode = 1   # starts with hidden lines visible, and the way back is refused
        original = self.view.SetDisplayMode3

        def sticky(use_parent, mode, faceted, edges):
            if self.view.set_calls.count(2) and mode == 1:  # the way back to hidden lines visible
                self.view.set_calls.append(mode)
                return True
            return original(use_parent, mode, faceted, edges)
        self.view.SetDisplayMode3 = sticky
        answer = self.check()
        self.assertFalse(answer["ok"])
        self.assertIn("did not come back", answer["message"])
        self.assertEqual(answer["data"]["display_mode"], "hidden_lines_removed")

    def test_an_unreadable_display_mode_changes_nothing(self) -> None:
        self.view.mode = None
        answer = self.check()
        self.assertFalse(answer["ok"])
        self.assertEqual(self.view.set_calls, [])

    def test_an_unreadable_edge_is_counted_not_guessed(self) -> None:
        self.view.hidden_edges.append(Edge(Curve((0, 0, 0), (0, 0, 0)), broken=True))
        answer = self.check()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["data"]["edges"]["unreadable"], 1)
        found = self.by_name(answer)
        # on a readable hidden edge, and on a drawn edge: still judged
        self.assertIs(found["D1@Bohrung@Teil.Part"]["into_void"], True)
        self.assertIs(found["D1@Skizze1@Teil.Part"]["into_void"], False)
        # on no readable edge: the unreadable model edge may carry it
        self.assertIsNone(found["D2@Skizze1@Teil.Part"]["into_void"])
        self.assertEqual(found["D2@Skizze1@Teil.Part"]["hidden_points"], [None, None])
        self.assertEqual((answer["data"]["into_void"], answer["data"]["unjudged"]), (1, 2))

    def test_no_model_edge_readable_gives_no_all_clear(self) -> None:
        broken = [Edge(e.curve, broken=True) for e in self.view.shown_edges + self.view.hidden_edges]
        self.view.ReferencedDocument.GetBodies2 = lambda kind, visible_only: (Body(broken),)
        answer = self.check()
        self.assertTrue(answer["ok"], answer)
        found = self.by_name(answer)
        # what lies on a drawn edge is not hidden; everything else is unjudged, never cleared
        self.assertIs(found["D1@Skizze1@Teil.Part"]["into_void"], False)
        for name in ("D1@Bohrung@Teil.Part", "D2@Skizze1@Teil.Part"):
            self.assertIsNone(found[name]["into_void"], name)
        self.assertEqual(answer["data"]["into_void"], 0)
        self.assertEqual(answer["data"]["edges"], {"all": 0, "shown": 3, "unreadable": 4})

    def test_an_unreadable_drawn_edge_leaves_a_hidden_looking_point_unjudged(self) -> None:
        self.view.shown_edges.append(Edge(Curve((0, 0, 0), (0, 0, 0)), broken=True))
        # the model reads its drawn edges and the hidden bore bottom
        self.view.ReferencedDocument.GetBodies2 = lambda kind, visible_only: (
            Body([e for e in self.view.shown_edges if not e.broken]
                 + [line((0.01, 0.005, 0), (0.02, 0.005, 0))]),)
        answer = self.check()
        self.assertTrue(answer["ok"], answer)
        depth = self.by_name(answer)["D1@Bohrung@Teil.Part"]
        self.assertEqual(depth["hidden_points"], [False, None])
        self.assertIsNone(depth["into_void"])

    def test_non_finite_points_give_no_verdict(self) -> None:
        self.dims["width"].points = [(0.0, 0.02, 0.0), (float("nan"), 0.02, 0.0)]
        self.dims["radius"].points = [(0.03, 0.01, 0.0)]
        found = self.by_name(self.check())
        self.assertIsNone(found["D1@Skizze1@Teil.Part"]["into_void"])
        self.assertIsNone(found["D1@Loch@Teil.Part"]["into_void"])

    def test_visible_edges_that_cannot_be_read_raise_and_the_mode_is_set_back(self) -> None:
        self.view.mode = 3

        def refuse(component, kind):
            raise OSError("GetVisibleEntities2 refused")
        self.view.GetVisibleEntities2 = refuse
        with self.assertRaises(OSError):
            self.check()
        self.assertEqual(self.view.mode, 3)
        self.assertEqual(self.view.set_calls, [2, 3])

    def test_bodies_that_cannot_be_read_raise_and_the_mode_is_kept(self) -> None:
        self.view.mode = 3

        def refuse(kind, visible_only):
            raise OSError("GetBodies2 refused")
        self.view.ReferencedDocument.GetBodies2 = refuse
        with self.assertRaises(OSError):
            self.check()
        self.assertEqual(self.view.mode, 3)

    def test_a_refused_dimension_walk_fails_and_the_mode_is_set_back(self) -> None:
        self.view.mode = 3

        def refuse():
            raise OSError("GetNext5 refused")
        self.view.displays[1].GetNext5 = refuse
        answer = self.check()
        self.assertFalse(answer["ok"])
        self.assertIn("GetNext5", answer["message"])
        self.assertEqual(self.view.mode, 3)
        self.assertEqual(self.view.set_calls, [2, 3])

    def test_a_view_without_transform_fails_and_is_set_back(self) -> None:
        self.view.ModelToViewTransform = None
        answer = self.check()
        self.assertFalse(answer["ok"])
        self.assertIn("model transform", answer["message"])
        self.assertEqual(self.view.mode, 2)

    def test_an_unknown_view_fails(self) -> None:
        answer = self.check(view_name="Drawing View9")
        self.assertFalse(answer["ok"])
        self.assertEqual(self.view.set_calls, [])

    def test_a_tolerance_must_be_positive(self) -> None:
        self.assertFalse(self.check(tolerance_mm=0)["ok"])
        self.assertEqual(self.view.set_calls, [])

    def test_a_wider_tolerance_finds_the_point_on_the_visible_edge(self) -> None:
        # the hidden bottom lies 5 mm above the visible base: with 6 mm tolerance it counts as shown
        self.assertIs(self.by_name(self.check(tolerance_mm=6))["D1@Bohrung@Teil.Part"]["into_void"], False)


if __name__ == "__main__":
    unittest.main()
