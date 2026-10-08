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

"""The plain-Python parts of the weldment module and the sketch resolution
it relies on: profile discovery, the 2D/3D sketch defaults, the rename
readback, the enum tables.  Runs under unittest discover, like CI."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks_mcp import sw_core, sw_weldment as wm  # noqa: E402


class FakeApp:
    """Only what profile_roots reads: the preference string and the install path."""

    def __init__(self, configured: str, install: Path) -> None:
        self._configured = configured
        self.GetExecutablePath = str(install)

    def GetUserPreferenceStringValue(self, index: int) -> str:  # noqa: N802 - COM member name
        assert index == wm.SW_FILE_LOCATIONS_WELDMENT_PROFILES
        return self._configured


def _profile(root: Path, standard: str, kind: str, size: str) -> Path:
    path = root / standard / kind / f"{size}{wm.PROFILE_SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


class ProfileDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_iter_profiles_reads_standard_type_size_sorted_and_prefers_the_configured_folder(self) -> None:
        custom = self.tmp / "custom"
        install = self.tmp / "install"
        _profile(custom, "iso", "square tube", "20 x 20 x 2")
        _profile(install / "lang" / "german" / "weldment profiles", "iso", "square tube", "20 x 20 x 2")
        _profile(install / "lang" / "german" / "weldment profiles", "iso", "pipe", "21.3 x 2.3")
        app = FakeApp(f"{custom};{self.tmp / 'missing'}", install)

        profiles = wm.iter_profiles(app)

        self.assertEqual(
            [(p["standard"], p["type"], p["size"]) for p in profiles],
            [("iso", "pipe", "21.3 x 2.3"), ("iso", "square tube", "20 x 20 x 2")],
        )
        square = next(p for p in profiles if p["type"] == "square tube")
        self.assertEqual(Path(square["path"]).parent.parent.parent, custom)

    def test_profile_roots_accepts_the_executable_path(self) -> None:
        install = self.tmp / "install"
        _profile(install / "lang" / "english" / "weldment profiles", "iso", "pipe", "21.3 x 2.3")
        app = FakeApp("", install / "sldworks.exe")
        self.assertEqual(wm.profile_roots(app), [install / "lang" / "english" / "weldment profiles"])

    def test_profile_roots_skips_missing_folders(self) -> None:
        app = FakeApp(str(self.tmp / "nowhere"), self.tmp / "no-install")
        self.assertEqual(wm.profile_roots(app), [])


class FakeFeature:
    """A feature-tree node as sw_core reads it: Name, GetTypeName2, GetNextFeature."""

    def __init__(self, name: str, type_name: str, taken: set[str] | None = None) -> None:
        self._name = name
        self.type_name = type_name
        self.taken = taken or set()
        self.next: FakeFeature | None = None

    @property
    def Name(self) -> str:  # noqa: N802 - COM member name
        return self._name

    @Name.setter
    def Name(self, value: str) -> None:  # noqa: N802 - COM member name
        if value in self.taken:
            raise RuntimeError("name already in use")
        self._name = value

    def GetTypeName2(self) -> str:  # noqa: N802 - COM member name
        return self.type_name

    def GetNextFeature(self):  # noqa: N802 - COM member name
        return self.next


class FakeTree:
    def __init__(self, *features: FakeFeature) -> None:
        for earlier, later in zip(features, features[1:]):
            earlier.next = later
        self.FirstFeature = features[0] if features else None


class SketchDefaultTests(unittest.TestCase):
    """A 3D path must never become the unnamed default of a profile feature."""

    def setUp(self) -> None:
        self.profile = FakeFeature("Sketch1", sw_core.SKETCH_2D_TYPE)
        self.boss = FakeFeature("Boss-Extrude1", "Extrusion")
        self.path = FakeFeature("3DSketch1", sw_core.SKETCH_3D_TYPE)
        self.doc = FakeTree(self.profile, self.boss, self.path)

    def test_latest_sketch_defaults_to_the_newest_2d_sketch(self) -> None:
        self.assertEqual(sw_core.latest_sketch(self.doc)[0], "Sketch1")
        self.assertEqual(sw_core.resolve_sketch(self.doc, None)[0], "Sketch1")

    def test_latest_sketch_includes_3d_only_when_asked(self) -> None:
        self.assertEqual(sw_core.latest_sketch(self.doc, include_3d=True)[0], "3DSketch1")
        self.assertEqual(sw_core.resolve_sketch(self.doc, None, include_3d=True)[0], "3DSketch1")

    def test_a_3d_sketch_resolves_by_its_explicit_name_for_path_readers(self) -> None:
        name, feature = sw_core.resolve_sketch(self.doc, "3DSketch1", include_3d=True)
        self.assertEqual((name, feature), ("3DSketch1", self.path))

    def test_a_named_3d_sketch_is_refused_where_a_profile_is_needed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "3D sketch"):
            sw_core.resolve_sketch(self.doc, "3DSketch1")
        self.assertEqual(sw_core.resolve_sketch(self.doc, "Sketch1")[0], "Sketch1")

    def test_a_non_sketch_name_is_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            sw_core.resolve_sketch(self.doc, "Boss-Extrude1")

    def test_sketch_names_lists_2d_by_default(self) -> None:
        self.assertEqual(sw_core.sketch_names(self.doc), ["Sketch1"])
        self.assertEqual(sw_core.sketch_names(self.doc, include_3d=True), ["Sketch1", "3DSketch1"])

    def test_list_sketches_shows_3d_paths_too(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from solidworks_mcp import sw_sketch

        self.doc.SketchManager = SimpleNamespace(ActiveSketch=None)
        with mock.patch.object(sw_sketch, "active_document", return_value=(None, self.doc)):
            listed = sw_sketch.list_sketches({})
        self.assertEqual(listed["data"]["sketches"], ["Sketch1", "3DSketch1"])


class OpenSketchNameTests(unittest.TestCase):
    """The open sketch is named by identity, not by being the newest."""

    def test_a_reopened_2d_sketch_keeps_its_own_name(self) -> None:
        profile = FakeFeature("Sketch1", sw_core.SKETCH_2D_TYPE)
        path = FakeFeature("3DSketch1", sw_core.SKETCH_3D_TYPE)
        profile.GetSpecificFeature2 = object()
        path.GetSpecificFeature2 = object()
        doc = FakeTree(profile, path)
        self.assertEqual(sw_core.open_sketch_name(doc, profile.GetSpecificFeature2), "Sketch1")
        self.assertEqual(sw_core.open_sketch_name(doc, path.GetSpecificFeature2), "3DSketch1")

    def test_an_unmatched_sketch_falls_back_to_the_newest(self) -> None:
        doc = FakeTree(FakeFeature("Sketch1", sw_core.SKETCH_2D_TYPE), FakeFeature("3DSketch1", sw_core.SKETCH_3D_TYPE))
        self.assertEqual(sw_core.open_sketch_name(doc, object()), "3DSketch1")


class RenameReadbackTests(unittest.TestCase):
    """create_3d_sketch reports the name the tree carries, not the one asked for."""

    def test_rename_returns_the_new_name_when_accepted(self) -> None:
        feature = FakeFeature("3DSketch1", sw_core.SKETCH_3D_TYPE)
        self.assertEqual(sw_core.rename_feature(feature, "Rahmen"), "Rahmen")
        self.assertEqual(feature.Name, "Rahmen")

    def test_rename_returns_the_old_name_when_refused(self) -> None:
        feature = FakeFeature("3DSketch1", sw_core.SKETCH_3D_TYPE, taken={"Rahmen"})
        self.assertEqual(sw_core.rename_feature(feature, "Rahmen"), "3DSketch1")
        self.assertEqual(feature.Name, "3DSketch1")

    def test_rename_without_a_name_or_feature_is_a_no_op(self) -> None:
        self.assertIsNone(sw_core.rename_feature(FakeFeature("x", "y"), None))
        self.assertIsNone(sw_core.rename_feature(None, "Rahmen"))


class FakeBodyWithMass:
    def __init__(self, volume_m3: float | None) -> None:
        self.volume_m3 = volume_m3

    def GetMassProperties(self, density: float):  # noqa: N802 - COM member name
        if self.volume_m3 is None:
            raise RuntimeError("no mass properties")
        return (0.0, 0.0, 0.0, self.volume_m3, 0.0, self.volume_m3 * density)


class BodyVolumeTests(unittest.TestCase):
    """list_bodies and the weldment tools report each body's own volume in mm³."""

    def test_body_volume_is_converted_to_cubic_millimetres(self) -> None:
        self.assertEqual(sw_core.body_volume_mm3(FakeBodyWithMass(2.0054866e-5)), 20054.866)

    def test_a_body_without_mass_properties_reports_none(self) -> None:
        self.assertIsNone(sw_core.body_volume_mm3(FakeBodyWithMass(None)))

    def _list_bodies(self, *volumes: float | None) -> dict:
        from unittest import mock

        from solidworks_mcp import sw_inspect

        context = [(FakeBodyWithMass(v), f"b{i}", None) for i, v in enumerate(volumes)]
        with mock.patch.object(sw_inspect, "active_document", return_value=(None, None)),                 mock.patch.object(sw_inspect, "iter_body_context", return_value=context):
            return sw_inspect.list_bodies({})["data"]

    def test_list_bodies_sums_when_every_body_has_a_volume(self) -> None:
        self.assertEqual(self._list_bodies(1e-6, 2e-6)["volume_sum_mm3"], 3000.0)

    def test_list_bodies_leaves_the_sum_unknown_when_a_volume_is_missing(self) -> None:
        data = self._list_bodies(1e-6, None)
        self.assertIsNone(data["volume_sum_mm3"])
        self.assertEqual(data["bodies"][0]["volume_mm3"], 1000.0)


class EnumTableTests(unittest.TestCase):
    def test_enum_tables_match_the_type_library(self) -> None:
        self.assertEqual(wm.CONNECTED_SEGMENTS, {"simple_cut": 1, "coped_cut": 2})
        self.assertEqual((wm.CORNER_TREATMENTS["miter"], wm.CORNER_TREATMENTS["butt1"]), (1, 2))
        self.assertEqual((wm.TRIM_COPED_CUT, wm.TRIM_WELD_GAP), (4, 8))
        self.assertEqual(wm.GUSSET_THICKNESS_DIRECTIONS, {"inner": 0, "both_sides": 1, "outer": 2})
        self.assertEqual(wm.GUSSET_LOCATIONS, {"start": 0, "center": 1, "end": 2})
        self.assertEqual(wm.GUSSET_PROFILES, {"triangle": False, "polygon": True})


if __name__ == "__main__":
    unittest.main()
