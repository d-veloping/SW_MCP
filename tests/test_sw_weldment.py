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

"""The plain-Python parts of the weldment module: profile discovery and the
enum tables."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("win32com")

from solidworks_mcp import sw_weldment as wm  # noqa: E402


class FakeApp:
    """Only what profile_roots reads: the preference string and the install path."""

    def __init__(self, configured: str, install: Path) -> None:
        self._configured = configured
        self.GetExecutablePath = str(install)

    def GetUserPreferenceStringValue(self, index: int) -> str:
        assert index == wm.SW_FILE_LOCATIONS_WELDMENT_PROFILES
        return self._configured


def _profile(root: Path, standard: str, kind: str, size: str) -> Path:
    path = root / standard / kind / f"{size}{wm.PROFILE_SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_iter_profiles_reads_standard_type_size_and_prefers_configured_folder(tmp_path: Path) -> None:
    custom = tmp_path / "custom"
    install = tmp_path / "install"
    _profile(custom, "iso", "square tube", "20 x 20 x 2")
    _profile(install / "lang" / "german" / "weldment profiles", "iso", "square tube", "20 x 20 x 2")
    _profile(install / "lang" / "german" / "weldment profiles", "iso", "pipe", "21.3 x 2.3")
    app = FakeApp(f"{custom};{tmp_path / 'missing'}", install)

    profiles = wm.iter_profiles(app)

    assert [(p["standard"], p["type"], p["size"]) for p in profiles] == [
        ("iso", "pipe", "21.3 x 2.3"),
        ("iso", "square tube", "20 x 20 x 2"),
    ]
    square = next(p for p in profiles if p["type"] == "square tube")
    assert Path(square["path"]).parent.parent.parent == custom


def test_profile_roots_skips_missing_folders(tmp_path: Path) -> None:
    app = FakeApp(str(tmp_path / "nowhere"), tmp_path / "no-install")
    assert wm.profile_roots(app) == []


def test_enum_tables_match_the_type_library() -> None:
    assert wm.CONNECTED_SEGMENTS == {"simple_cut": 1, "coped_cut": 2}
    assert wm.CORNER_TREATMENTS["miter"] == 1 and wm.CORNER_TREATMENTS["butt1"] == 2
    assert wm.TRIM_COPED_CUT == 4 and wm.TRIM_WELD_GAP == 8
