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

"""The plain-Python parts of the sheet metal module: DXF summary, export
option bits, the server-fault check.  Runs under unittest discover, like CI."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_sheetmetal as sm  # noqa: E402


def _entity(kind: str, groups: list[tuple[str, object]]) -> list[str]:
    lines = ["  0", kind, "  8", "0"]
    for code, raw in groups:
        lines += [f"{int(code):3d}", str(raw)]
    return lines


def _line(x1: float, y1: float, x2: float, y2: float, linetype: str = "Continuous") -> list[str]:
    return _entity("LINE", [("6", linetype), ("10", x1), ("20", y1), ("11", x2), ("21", y2)])


def _dxf(entities: list[list[str]]) -> str:
    """A minimal DXF with a HEADER and an ENTITIES section."""
    lines = ["  0", "SECTION", "  2", "HEADER", "  9", "$ACADVER", "  1", "AC1015", "  0", "ENDSEC",
             "  0", "SECTION", "  2", "ENTITIES"]
    for entity in entities:
        lines += entity
    lines += ["  0", "ENDSEC", "  0", "EOF"]
    return "\n".join(lines) + "\n"


class DxfSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "flat.dxf"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def summarize(self, text: str) -> dict:
        self.path.write_text(text, encoding="utf-8")
        return sm.summarize_dxf(self.path)

    def test_counts_bend_lines_by_linetype(self) -> None:
        summary = self.summarize(_dxf([
            _line(0, 0, 40, 0), _line(40, 0, 40, 81), _line(40, 81, 0, 81), _line(0, 81, 0, 0),
            _line(0, 49, 40, 49, "CENTERX2"),
        ]))
        self.assertEqual(summary["entities"], {"LINE": 5})
        self.assertEqual(summary["bend_lines"], 1)
        self.assertEqual(summary["outline_entities"], 4)
        self.assertEqual(summary["extents_mm"], [40.0, 81.0])

    def test_ignores_other_sections_and_the_eof_marker(self) -> None:
        text = _dxf([_line(0, 0, 1, 1)]).replace("  9\n$ACADVER", "  0\nLINE\n  9\n$ACADVER")
        self.assertEqual(self.summarize(text)["entities"], {"LINE": 1})

    def test_circle_extents_use_the_radius_not_the_centre(self) -> None:
        """A hole in a flat pattern: its centre is not an outline point."""
        summary = self.summarize(_dxf([
            _line(0, 0, 100, 0),
            _entity("CIRCLE", [("10", 50), ("20", 30), ("40", 10)]),
        ]))
        self.assertEqual(summary["extents_mm"], [100.0, 40.0])
        self.assertEqual(summary["outline_entities"], 2)

    def test_arc_extents_include_the_axis_crossings_inside_the_sweep(self) -> None:
        """A rounded corner: quarter arc from 0 to 90 degrees about (10, 10), r = 10.

        Its ends are (20, 10) and (10, 20); nothing of it reaches x = 0 or
        y = 0, so the extents are 10 by 10, not 20 by 20 from the centre.
        """
        summary = self.summarize(_dxf([
            _entity("ARC", [("10", 10), ("20", 10), ("40", 10), ("50", 0), ("51", 90)]),
        ]))
        self.assertEqual(summary["extents_mm"], [10.0, 10.0])
        # A three-quarter arc from 90 to 0 degrees does cross 180 and 270.
        summary = self.summarize(_dxf([
            _entity("ARC", [("10", 10), ("20", 10), ("40", 10), ("50", 90), ("51", 0)]),
        ]))
        self.assertEqual(summary["extents_mm"], [20.0, 20.0])

    def test_polyline_keeps_every_vertex(self) -> None:
        summary = self.summarize(_dxf([
            _entity("LWPOLYLINE", [("90", 3), ("10", 0), ("20", 0), ("10", 30), ("20", 0), ("10", 30), ("20", 12)]),
        ]))
        self.assertEqual(summary["extents_mm"], [30.0, 12.0])

    def test_a_spline_leaves_the_extents_out(self) -> None:
        summary = self.summarize(_dxf([
            _line(0, 0, 5, 5),
            _entity("SPLINE", [("10", 0), ("20", 0), ("10", 50), ("20", 80), ("10", 100), ("20", 0)]),
        ]))
        self.assertNotIn("extents_mm", summary)
        self.assertIn("extents_note", summary)

    def test_an_ellipse_leaves_the_extents_out(self) -> None:
        summary = self.summarize(_dxf([
            _line(0, 0, 5, 5),
            _entity("ELLIPSE", [("10", 50), ("20", 0), ("11", 30), ("21", 0), ("40", 0.5)]),
        ]))
        self.assertNotIn("extents_mm", summary)
        self.assertEqual(summary["outline_entities"], 2)

    def test_a_bulged_polyline_leaves_the_extents_out(self) -> None:
        summary = self.summarize(_dxf([
            _entity("LWPOLYLINE", [("90", 2), ("10", 0), ("20", 0), ("42", 1), ("10", 20), ("20", 0)]),
        ]))
        self.assertNotIn("extents_mm", summary)

    def test_a_zero_bulge_keeps_the_extents(self) -> None:
        summary = self.summarize(_dxf([
            _entity("LWPOLYLINE", [("90", 2), ("10", 0), ("20", 0), ("42", 0), ("10", 20), ("20", 7)]),
        ]))
        self.assertEqual(summary["extents_mm"], [20.0, 7.0])

    def test_unknown_entities_do_not_pollute_the_extents(self) -> None:
        summary = self.summarize(_dxf([
            _line(0, 0, 5, 5),
            _entity("TEXT", [("10", 500), ("20", 500), ("1", "note")]),
        ]))
        self.assertEqual(summary["extents_mm"], [5.0, 5.0])


class EdgeFlangeCleanupTests(unittest.TestCase):
    """A failing later edge must not leave the earlier profile sketches behind."""

    def test_earlier_profiles_are_deleted_when_a_later_edge_fails(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        doc = SimpleNamespace(SketchManager=SimpleNamespace(ActiveSketch=None))
        first = object()
        draws = [(first, object()), RuntimeError("no profile for that edge")]

        def draw(*_args):
            outcome = draws.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(sm, "require_part", return_value=(None, doc)),                 mock.patch.object(sm, "_require_sheet_metal"),                 mock.patch.object(sm, "_edge_objects", return_value=["e1", "e2"]),                 mock.patch.object(sm, "_draw_flange_profile", side_effect=draw),                 mock.patch.object(sm, "_delete_features") as delete:
            with self.assertRaises(RuntimeError):
                sm.sheet_metal_edge_flange({"selection": {"edges": [1, 2]}, "length_mm": 10})
        delete.assert_called_once_with(doc, [first])


class FakeFlatPattern:
    def __init__(self, suppressed: bool) -> None:
        self.IsSuppressed = suppressed


class FlatPatternTests(unittest.TestCase):
    """One Flat-Pattern per body: the single-body tools refuse a multibody part."""

    def _with(self, *features: FakeFlatPattern):
        from unittest import mock

        return mock.patch.object(sm, "_features_of_type", return_value=list(features))

    def test_a_single_flat_pattern_is_returned(self) -> None:
        only = FakeFlatPattern(True)
        with self._with(only):
            self.assertIs(sm._single_flat_pattern(None), only)
        with self._with():
            self.assertIsNone(sm._single_flat_pattern(None))

    def test_a_multibody_part_is_refused(self) -> None:
        with self._with(FakeFlatPattern(True), FakeFlatPattern(True)):
            with self.assertRaisesRegex(RuntimeError, "2 Flat-Pattern features"):
                sm._single_flat_pattern(None)

    def test_flat_means_every_flat_pattern_is_unsuppressed(self) -> None:
        with self._with(FakeFlatPattern(False), FakeFlatPattern(False)):
            self.assertTrue(sm._is_flattened(None))
        with self._with(FakeFlatPattern(False), FakeFlatPattern(True)):
            self.assertFalse(sm._is_flattened(None))
        with self._with():
            self.assertFalse(sm._is_flattened(None))


class OptionTests(unittest.TestCase):
    def test_export_options_default_to_geometry_and_bend_lines(self) -> None:
        self.assertEqual(sm.export_options({}), sm.EXPORT_GEOMETRY | sm.EXPORT_BEND_LINES)
        self.assertEqual(sm.export_options({"bend_lines": False}), sm.EXPORT_GEOMETRY)
        self.assertEqual(
            sm.export_options({"sketches": True, "hidden_edges": True}),
            sm.EXPORT_GEOMETRY | sm.EXPORT_BEND_LINES | sm.EXPORT_SKETCHES | sm.EXPORT_HIDDEN_EDGES,
        )

    def test_is_server_fault_reads_hresult_or_first_arg(self) -> None:
        class WithHresult(Exception):
            hresult = sm._SERVER_FAULT

        self.assertTrue(sm.is_server_fault(WithHresult()))
        self.assertTrue(sm.is_server_fault(Exception(sm._SERVER_FAULT, "Ausnahmefehler des Servers.")))
        self.assertFalse(sm.is_server_fault(Exception(-2147352571, "Typenkonflikt.")))
        self.assertFalse(sm.is_server_fault(RuntimeError("nope")))

    def test_enum_tables_match_the_type_library(self) -> None:
        self.assertEqual(sm.FLANGE_POSITIONS, {"material_inside": 1, "material_outside": 2, "bend_outside": 3})
        self.assertEqual((sm.HEM_TYPES["closed"], sm.HEM_TYPES["rolled"]), (1, 3))
        self.assertEqual(sm.CLOSED_CORNER_TYPES, {"butt": 1, "overlap": 2, "underlap": 3})
        self.assertEqual(sm.RELIEF_TYPES["none"], 4)
        self.assertEqual(sm.CORNER_RELIEF_TYPES["square"], 1)


if __name__ == "__main__":
    unittest.main()
