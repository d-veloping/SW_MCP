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

"""Offline tests for the display style of drawing views (issue #4).

The fake view behaves like IView on SOLIDWORKS 2016 SP3 as measured: one
swDisplayMode_e for SetDisplayMode3 and GetDisplayMode2, Edges kept only for
shaded, and modes 4 and 5 accepted with True but stored as 0 and 1.
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


class FakeView:
    def __init__(self, name: str, kind: int = 4, broken: bool = False) -> None:
        self.name = name
        self.Type = kind
        self.mode = 2            # hidden lines removed, the default after placing
        self.edges = False
        self.broken = broken     # accepts every call, keeps its mode: a refused style
        self.calls: list[tuple] = []
        self.next: FakeView | None = None
        self.ScaleDecimal = 1.0

    def _FlagAsMethod(self, *names: str) -> None:  # noqa: N802 - pywin32 member name
        pass

    def GetName2(self) -> str:  # noqa: N802 - COM member name
        return self.name

    def GetNextView(self):  # noqa: N802 - COM member name
        return self.next

    def SetDisplayMode3(self, use_parent, mode, facetted, edges):  # noqa: N802 - COM member name
        self.calls.append((use_parent, mode, facetted, edges))
        if not self.broken:
            self.mode = mode % 4
            if self.mode == 3:
                self.edges = bool(edges)
        return True

    def GetDisplayMode2(self) -> int:  # noqa: N802 - COM member name
        return self.mode

    def GetDisplayEdgesInShadedMode(self) -> bool:  # noqa: N802 - COM member name
        return self.edges


class FakeDrawing:
    def __init__(self, views: list[FakeView]) -> None:
        self.views = views
        self.created: list[FakeView] = []
        self.next_view: FakeView | None = None

    def _link(self) -> None:
        for a, b in zip(self.views, self.views[1:] + [None]):
            a.next = b

    def GetFirstView(self):  # noqa: N802 - COM member name
        self._link()
        return self.views[0] if self.views else None

    def Create1stAngleViews2(self, model: str) -> bool:  # noqa: N802 - COM member name
        start = len([v for v in self.views if v.Type != 1]) + 1
        new = [FakeView(f"Drawing View{start + i}") for i in range(3)]
        self.created = new
        self.views.extend(new)
        return True

    def CreateDrawViewFromModelView3(self, model, name, x, y, z):  # noqa: N802 - COM member name
        view = self.next_view or FakeView("Drawing View9")
        self.views.append(view)
        return view

    def ActivateView(self, name: str) -> bool:  # noqa: N802 - COM member name
        return True

    def CreateUnfoldedViewAt3(self, x, y, z, not_aligned):  # noqa: N802 - COM member name
        view = self.next_view or FakeView("Drawing View8")
        self.views.append(view)
        return view


class DisplayModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sheet = FakeView("Sheet1", kind=1)
        self.existing = FakeView("Drawing View1")
        self.doc = FakeDrawing([self.sheet, self.existing])
        for patch in (
            unittest.mock.patch.object(sw_drawing, "require_drawing", return_value=(None, self.doc)),
            unittest.mock.patch.object(sw_drawing, "_open_model_path", return_value="C:/m/teil.sldprt"),
            unittest.mock.patch.object(sw_drawing, "_model_view_name", return_value="*Front"),
            unittest.mock.patch.object(sw_drawing, "rebuild"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_each_name_maps_to_the_measured_call(self) -> None:
        expected = {
            "wireframe": (False, 0, False, False),
            "hidden_lines_visible": (False, 1, False, False),
            "hidden_lines_removed": (False, 2, False, False),
            "shaded": (False, 3, False, False),
            "shaded_with_edges": (False, 3, False, True),
            "hidden_lines_grey": (False, 1, False, False),
        }
        for name, call in expected.items():
            with self.subTest(name=name):
                view = FakeView("V")
                outcome = sw_drawing._set_display_mode(view, name)
                self.assertEqual(view.calls, [call])
                self.assertTrue(outcome["ok"], outcome)
                self.assertEqual(outcome["actual"], sw_drawing.DISPLAY_MODE_ALIASES.get(name, name))

    def test_readback_names(self) -> None:
        view = FakeView("V")
        for mode, edges, name in ((0, False, "wireframe"), (1, False, "hidden_lines_visible"),
                                  (2, False, "hidden_lines_removed"), (3, False, "shaded"), (3, True, "shaded_with_edges")):
            view.mode, view.edges = mode, edges
            self.assertEqual(sw_drawing._display_mode(view), name)

    def test_a_true_return_without_readback_is_a_failure(self) -> None:
        # the measured case: SetDisplayMode3 answers True and the view keeps another mode
        view = FakeView("V", broken=True)
        outcome = sw_drawing._set_display_mode(view, "hidden_lines_visible")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["actual"], "hidden_lines_removed")
        self.assertFalse(sw_drawing._set_display_mode(FakeView("V"), "dotted")["ok"])

    def test_the_alias_is_accepted_but_not_a_display_name(self) -> None:
        self.assertNotIn("hidden_lines_grey", sw_drawing.DISPLAY_MODES)
        self.assertIn("hidden_lines_grey", sw_drawing.DISPLAY_MODE_SCHEMA["enum"])

    def test_standard_views_restyle_only_the_views_they_place(self) -> None:
        answer = sw_drawing.insert_standard_views({"display_mode": "hidden_lines_visible"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([v.mode for v in self.doc.created], [1, 1, 1])
        self.assertEqual(self.existing.calls, [])
        self.assertEqual(self.sheet.calls, [])
        self.assertEqual([o["view"] for o in answer["data"]["display"]], [v.name for v in self.doc.created])
        modes = {v["name"]: v.get("display_mode") for v in answer["data"]["views"]}
        self.assertEqual(modes["Drawing View1"], "hidden_lines_removed")
        self.assertNotIn("display_mode", [k for v in answer["data"]["views"] if v["name"] == "Sheet1" for k in v])

    def test_standard_views_fail_when_one_view_keeps_its_mode(self) -> None:
        original = self.doc.Create1stAngleViews2

        def create(model: str) -> bool:
            ok = original(model)
            self.doc.created[1].broken = True
            return ok
        self.doc.Create1stAngleViews2 = create
        answer = sw_drawing.insert_standard_views({"display_mode": "hidden_lines_visible"})
        self.assertFalse(answer["ok"])
        self.assertIn(self.doc.created[1].name, answer["message"])
        self.assertEqual([o["ok"] for o in answer["data"]["display"]], [True, False, True])

    def test_without_display_mode_nothing_is_restyled(self) -> None:
        answer = sw_drawing.insert_standard_views({})
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(all(v.calls == [] for v in self.doc.views))
        self.assertNotIn("display", answer["data"])

    def test_model_view_projected_view_and_set_drawing_view(self) -> None:
        for tool, args in ((sw_drawing.insert_model_view, {"x_mm": 100, "y_mm": 100}),
                           (sw_drawing.insert_projected_view, {"parent_view": "Drawing View1"}),
                           (sw_drawing.set_drawing_view, {"name": "Drawing View1"})):
            with self.subTest(tool=tool.__name__):
                target = self.existing if tool is sw_drawing.set_drawing_view else FakeView("Neu")
                self.doc.next_view = target
                answer = tool({**args, "display_mode": "shaded_with_edges"})
                self.assertTrue(answer["ok"], answer)
                self.assertEqual(answer["data"]["view"]["display_mode"], "shaded_with_edges")
                target.broken, target.mode = True, 2
                answer = tool({**args, "display_mode": "wireframe"})
                self.assertFalse(answer["ok"], answer)
                self.assertEqual(answer["data"]["display"][0]["actual"], "hidden_lines_removed")
                target.broken = False


if __name__ == "__main__":
    unittest.main()
