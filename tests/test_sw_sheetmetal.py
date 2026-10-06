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
option bits, the server-fault check."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("win32com")

from solidworks_mcp import sw_sheetmetal as sm  # noqa: E402


def _dxf(entities: list[tuple[str, str, tuple[float, float], tuple[float, float]]]) -> str:
    """A minimal DXF with a HEADER and an ENTITIES section of LINEs."""
    lines = ["  0", "SECTION", "  2", "HEADER", "  9", "$ACADVER", "  1", "AC1015", "  0", "ENDSEC",
             "  0", "SECTION", "  2", "ENTITIES"]
    for kind, linetype, (x1, y1), (x2, y2) in entities:
        lines += ["  0", kind, "  8", "0", "  6", linetype, " 10", str(x1), " 20", str(y1), " 11", str(x2), " 21", str(y2)]
    lines += ["  0", "ENDSEC", "  0", "EOF"]
    return "\n".join(lines) + "\n"


def test_summarize_dxf_counts_bend_lines_by_linetype(tmp_path: Path) -> None:
    path = tmp_path / "flat.dxf"
    path.write_text(_dxf([
        ("LINE", "Continuous", (0, 0), (40, 0)),
        ("LINE", "Continuous", (40, 0), (40, 81)),
        ("LINE", "Continuous", (40, 81), (0, 81)),
        ("LINE", "Continuous", (0, 81), (0, 0)),
        ("LINE", "CENTERX2", (0, 49), (40, 49)),
    ]), encoding="utf-8")
    summary = sm.summarize_dxf(path)
    assert summary["entities"] == {"LINE": 5}
    assert summary["bend_lines"] == 1
    assert summary["outline_entities"] == 4
    assert summary["extents_mm"] == [40.0, 81.0]


def test_summarize_dxf_ignores_other_sections(tmp_path: Path) -> None:
    text = _dxf([("LINE", "Continuous", (0, 0), (1, 1))])
    # A LINE-looking pair inside the HEADER must not count.
    text = text.replace("  9\n$ACADVER", "  0\nLINE\n  9\n$ACADVER")
    path = tmp_path / "flat.dxf"
    path.write_text(text, encoding="utf-8")
    assert sm.summarize_dxf(path)["entities"] == {"LINE": 1}


def test_export_options_default_to_geometry_and_bend_lines() -> None:
    assert sm.export_options({}) == sm.EXPORT_GEOMETRY | sm.EXPORT_BEND_LINES
    assert sm.export_options({"bend_lines": False}) == sm.EXPORT_GEOMETRY
    assert sm.export_options({"sketches": True, "hidden_edges": True}) == (
        sm.EXPORT_GEOMETRY | sm.EXPORT_BEND_LINES | sm.EXPORT_SKETCHES | sm.EXPORT_HIDDEN_EDGES
    )


def test_is_server_fault_reads_hresult_or_first_arg() -> None:
    class WithHresult(Exception):
        hresult = sm._SERVER_FAULT

    assert sm.is_server_fault(WithHresult())
    assert sm.is_server_fault(Exception(sm._SERVER_FAULT, "Ausnahmefehler des Servers."))
    assert not sm.is_server_fault(Exception(-2147352571, "Typenkonflikt."))
    assert not sm.is_server_fault(RuntimeError("nope"))


def test_enum_tables_match_the_type_library() -> None:
    assert sm.FLANGE_POSITIONS == {"material_inside": 1, "material_outside": 2, "bend_outside": 3}
    assert sm.HEM_TYPES["closed"] == 1 and sm.HEM_TYPES["rolled"] == 3
    assert sm.CLOSED_CORNER_TYPES == {"butt": 1, "overlap": 2, "underlap": 3}
    assert sm.RELIEF_TYPES["none"] == 4
