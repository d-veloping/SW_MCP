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

"""Weldments: 3D sketches, structural members from profile libraries, end
caps, trim/extend, and gussets.

Measured on SOLIDWORKS 2016 SP3 (ClauSW, 2026-10-06): a structural member is
one IStructuralMemberGroup whose Segments are the sketch segments of a 3D (or
2D) sketch, passed to InsertStructuralWeldment4 with the library profile path.
Connected segments in one group are mitred, so the chain's volume is the
profile area times the sum of the segment lengths -- which is how the live
test checks it.  Trimming against a body shortens the trimmed member and
grows the trimming member by the same amount, whatever the extension option
bits say (measured with 0, 1, 2 and 3), so the part's total volume does not
move; bodies are therefore measured one at a time through
IBody2::GetMassProperties.
A gusset (InsertGussetFeature3) takes its two supporting faces with mark 1;
a triangle of legs a and b at thickness t measures a*b*t/2 exactly, a polygon
a*b*t minus the cut-off corner.  Its chamfer and plane-offset arguments were
accepted and changed nothing here, so the tool does not offer them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .sw_core import (
    apply_selection,
    as_list,
    body_extents,
    body_volume_mm3,
    box_mm,
    clear_selection,
    dispatch_array,
    exit_active_sketch,
    extension,
    feature_manager,
    feature_property,
    feature_result,
    flag_methods,
    get_bodies,
    iter_features,
    latest_sketch,
    logger,
    rename_feature,
    require_part,
    require_selection,
    result,
    running_app,
    safe,
    selectable,
    selected_objects,
    SELECTION_SCHEMA,
    sketch_manager,
    sketch_segment_objects,
    to_deg,
    to_m,
    to_mm,
    to_rad,
    tool,
    value,
    volume_total_mm3,
)


# swConnectedSegmentsOption_e
CONNECTED_SEGMENTS = {"simple_cut": 1, "coped_cut": 2}
# swSolidworksWeldmentEndCondOptions_e
CORNER_TREATMENTS = {"none": 0, "miter": 1, "butt1": 2, "butt2": 3, "trim": 4}
# Corners within a member group: trim needs a trimming boundary and belongs
# to weldment_trim_extend.
MEMBER_CORNER_TREATMENTS = ("butt1", "butt2", "miter", "none")
# swGussetThicknessType_e / swGussetProfileLocationType_e / swGussetProfileType_e
GUSSET_THICKNESS_DIRECTIONS = {"inner": 0, "both_sides": 1, "outer": 2}
GUSSET_LOCATIONS = {"start": 0, "center": 1, "end": 2}
GUSSET_PROFILES = {"triangle": False, "polygon": True}
# swWeldmentTrimExtendOptionType_e
TRIM_ALLOW_TRIMMED_EXTENSION = 1
TRIM_ALLOW_TRIMMING_EXTENSION = 2
TRIM_COPED_CUT = 4
TRIM_WELD_GAP = 8

# swUserPreferenceStringValue_e.swFileLocationsWeldmentProfiles
SW_FILE_LOCATIONS_WELDMENT_PROFILES = 29
PROFILE_SUFFIX = ".sldlfp"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _feature_names(doc: Any) -> list[str]:
    return [f["name"] for f in iter_features(doc)]


def body_summary(body: Any) -> dict[str, Any]:
    """Name, exact bounding box and volume of one body, measured on the body itself."""
    entry: dict[str, Any] = {"name": str(safe(body, "Name", "") or "")}
    box = body_extents(body)
    if box is not None:
        entry.update(box_mm(box))
    volume = body_volume_mm3(body)
    if volume is not None:
        entry["volume_mm3"] = volume
    return entry


def bodies_summary(doc: Any) -> list[dict[str, Any]]:
    return [dict(body_summary(body), index=index) for index, body in enumerate(get_bodies(doc))]


def bodies_unchanged(before: dict[str, dict[str, Any]], after: list[dict[str, Any]]) -> bool:
    """True when a feature provably left every body as it was.

    The judge is the per-body volume: a trim that cuts moves volume between
    the two members, a trim that splits adds a body, and SOLIDWORKS renames
    the bodies it touched.  Boxes are not consulted, and a body whose volume
    is unknown on either side cannot be called unchanged.
    """
    if len(after) != len(before):
        return False
    for body in after:
        previous = before.get(body["name"])
        if previous is None or "volume_mm3" not in previous or "volume_mm3" not in body:
            return False
        if abs(previous["volume_mm3"] - body["volume_mm3"]) > 1e-3:
            return False
    return True


def trim_count_error(corner_type: str, targets: int, boundaries: int) -> str | None:
    """Why a trim with these counts cannot be honoured, or None.

    Only the end trim (corner_type trim) takes several target or boundary
    bodies; butt and miter act on one member against one body.  Measured on
    2016 SP3: two targets with butt1 or miter trim neither of them.
    """
    if corner_type == "trim" or (targets <= 1 and boundaries <= 1):
        return None
    return (
        f"corner_type {corner_type} trims one member against one body; got {targets} bodies and {boundaries} "
        "boundaries. Call once per member, or use corner_type trim for several at once."
    )


def _ensure_weldment(doc: Any) -> Any:
    """Add the Weldment feature when the part has none.

    Returns the added feature, or None when the part already was a weldment,
    so a failed member can take the feature back out again.
    """
    if bool(safe(doc, "IsWeldment", False)):
        return None
    feature = feature_manager(doc).InsertWeldmentFeature()
    if not bool(safe(doc, "IsWeldment", False)):
        raise RuntimeError("SOLIDWORKS did not turn the part into a weldment.")
    return feature


def _remove_weldment(doc: Any, feature: Any) -> bool:
    """Delete a Weldment feature this call added; True if the part is plain again."""
    clear_selection(doc)
    try:
        if bool(selectable(feature).Select2(False, 0)):
            extension(doc).DeleteSelection2(0)
    except Exception:
        logger.info("Could not remove the Weldment feature added for a failed member")
    clear_selection(doc)
    return not bool(safe(doc, "IsWeldment", False))


def _resolve_profile(app: Any, args: dict[str, Any]) -> Path:
    explicit = args.get("profile_path")
    if explicit:
        path = Path(str(explicit)).expanduser()
        if path.suffix.lower() != PROFILE_SUFFIX or not path.is_file():
            raise RuntimeError(f"profile_path must be an existing {PROFILE_SUFFIX} file; {path} is not.")
        return path
    standard, kind, size = (str(args.get(k) or "").strip().lower() for k in ("standard", "type", "size"))
    if not (standard and kind and size):
        raise RuntimeError("Pass profile_path, or standard, type and size as list_weldment_profiles reports them.")
    for profile in iter_profiles(app):
        if (profile["standard"].lower(), profile["type"].lower(), profile["size"].lower()) == (standard, kind, size):
            return Path(profile["path"])
    raise RuntimeError(f"No weldment profile {standard}/{kind}/{size}; call list_weldment_profiles for the available ones.")


def profile_roots(app: Any) -> list[Path]:
    """Where profile libraries live: the configured folders, then the install."""
    roots: list[Path] = []
    try:
        configured = str(app.GetUserPreferenceStringValue(SW_FILE_LOCATIONS_WELDMENT_PROFILES) or "")
    except Exception:
        configured = ""
    roots.extend(Path(p.strip()) for p in configured.split(";") if p.strip())
    try:
        install = Path(str(value(app, "GetExecutablePath") or ""))
    except Exception:
        install = Path()
    # 2016 SP3 returns the install folder (measured 2026-10-08); the API
    # documents the path of sldworks.exe, so a file path means its folder.
    if install.suffix.lower() == ".exe":
        install = install.parent
    if install.parts:
        for lang in sorted((install / "lang").glob("*")) if (install / "lang").is_dir() else []:
            roots.append(lang / "weldment profiles")
    seen: set[str] = set()
    unique = []
    for root in roots:
        key = str(root).lower()
        if key not in seen and root.is_dir():
            seen.add(key)
            unique.append(root)
    return unique


def iter_profiles(app: Any) -> list[dict[str, str]]:
    """Every <root>/<standard>/<type>/<size>.sldlfp, sorted; the first root wins on duplicates."""
    profiles: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for root in profile_roots(app):
        for path in sorted(root.glob(f"*/*/*{PROFILE_SUFFIX}")):
            key = (path.parts[-3].lower(), path.parts[-2].lower(), path.stem.lower())
            if key in seen:
                continue
            seen.add(key)
            profiles.append({"standard": path.parts[-3], "type": path.parts[-2], "size": path.stem, "path": str(path)})
    return sorted(profiles, key=lambda p: (p["standard"].lower(), p["type"].lower(), p["size"].lower()))


def _segment_objects(doc: Any, selection: dict[str, Any] | None) -> list[Any]:
    spec = selection or {}
    indices = spec.get("sketch_segments")
    if not indices:
        raise RuntimeError("selection.sketch_segments is required: the segment indices of the path sketch from list_sketch_segments.")
    sketch_name = spec.get("sketch_name")
    if not sketch_name and doc.SketchManager.ActiveSketch is None:
        # The unnamed default of a path is the newest sketch of either kind;
        # the 2D-only default belongs to profile features.
        sketch_name, _ = latest_sketch(doc, include_3d=True)
    segments = sketch_segment_objects(doc, sketch_name)
    chosen = []
    for raw in indices:
        index = int(raw)
        if not 0 <= index < len(segments):
            raise RuntimeError(f"Sketch segment {index} is out of range (0..{len(segments) - 1}).")
        chosen.append(segments[index])
    return chosen


def _body_objects(doc: Any, indices: list[int]) -> list[Any]:
    bodies = get_bodies(doc)
    chosen = []
    for raw in indices:
        index = int(raw)
        if not 0 <= index < len(bodies):
            raise RuntimeError(f"Body index {index} is out of range (0..{len(bodies) - 1}); call list_bodies first.")
        chosen.append(bodies[index])
    return chosen


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@tool(
    "create_3d_sketch",
    "Create a 3D sketch of straight lines, given as model-space end points in millimetres, and close it. "
    "Lines that share an end point are connected; the sketch is the path for weldment_structural_member. "
    "Returns the sketch name and one segment index per line, in the order given.",
    {
        "lines": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "properties": {k: {"type": "number"} for k in ("x1_mm", "y1_mm", "z1_mm", "x2_mm", "y2_mm", "z2_mm")},
                "required": ["x1_mm", "y1_mm", "z1_mm", "x2_mm", "y2_mm", "z2_mm"],
            },
        },
        "name": {"type": "string", "description": "Rename the sketch once created."},
    },
    ["lines"],
)
def create_3d_sketch(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    clear_selection(doc)
    manager = flag_methods(sketch_manager(doc), "Insert3DSketch")
    before = _feature_names(doc)
    manager.Insert3DSketch(True)
    if doc.SketchManager.ActiveSketch is None:
        return result(False, "SOLIDWORKS did not open a 3D sketch.")
    created = 0
    try:
        manager.AddToDB = True
        for line in args["lines"]:
            segment = manager.CreateLine(
                to_m(line["x1_mm"]), to_m(line["y1_mm"]), to_m(line["z1_mm"]),
                to_m(line["x2_mm"]), to_m(line["y2_mm"]), to_m(line["z2_mm"]),
            )
            if segment is not None:
                created += 1
    finally:
        manager.AddToDB = False
        manager.Insert3DSketch(True)
    new = [n for n in _feature_names(doc) if n not in before]
    if not new:
        return result(False, "The 3D sketch was not added to the feature tree.")
    name = new[-1]
    from .sw_core import find_feature

    feature = find_feature(doc, name)
    wanted = args.get("name")
    # The name reported is the one the tree carries, which differs from the
    # wanted one when SOLIDWORKS refuses the rename (a taken name, say).
    name = rename_feature(feature, wanted) or name
    segments = len(as_list(safe(value(feature, "GetSpecificFeature2"), "GetSketchSegments")))
    ok = segments == len(args["lines"])
    message = (
        f"Created 3D sketch '{name}' with {segments} segments." if ok
        else f"Created 3D sketch '{name}', but it holds {segments} segments instead of {len(args['lines'])}."
    )
    if wanted and name != str(wanted):
        message += f" SOLIDWORKS kept the name '{name}' instead of '{wanted}'."
    return result(ok, message, sketch=name, segments=list(range(segments)), created=created)


@tool(
    "list_weldment_profiles",
    "Read-only: the structural member profiles available on this install, as standard / type / size "
    "with the .sldlfp path each, from the configured profile folders and the SOLIDWORKS install.",
    {
        "standard": {"type": "string", "description": "Only this standard, e.g. iso or 'ansi inch'."},
        "type": {"type": "string", "description": "Only this profile type, e.g. 'square tube'."},
    },
)
def list_weldment_profiles(args: dict[str, Any]) -> dict[str, Any]:
    app = running_app()
    profiles = iter_profiles(app)
    standard = str(args.get("standard") or "").lower()
    kind = str(args.get("type") or "").lower()
    if standard:
        profiles = [p for p in profiles if p["standard"].lower() == standard]
    if kind:
        profiles = [p for p in profiles if p["type"].lower() == kind]
    return result(
        bool(profiles),
        f"{len(profiles)} profiles." if profiles else "No weldment profiles found; check Tools > Options > File Locations > Weldment Profiles.",
        profiles=profiles, roots=[str(r) for r in profile_roots(app)],
    )


@tool(
    "weldment_structural_member",
    "Sweep a library profile along sketch segments (a 3D sketch from create_3d_sketch, or a 2D one) to make "
    "structural members, one body per segment. Pick the profile by standard, type and size from "
    "list_weldment_profiles, or give profile_path. Connected segments in one call form one group: their "
    "corners are mitred (corner_treatment), and the whole chain's volume is profile area times total "
    "length. Segments that are not connected or not parallel need separate calls. Adds the Weldment "
    "feature to the part if it has none.",
    {
        "selection": SELECTION_SCHEMA,
        "standard": {"type": "string"},
        "type": {"type": "string"},
        "size": {"type": "string"},
        "profile_path": {"type": "string", "description": "Full path of a .sldlfp profile instead of standard/type/size."},
        "corner_treatment": {"type": "string", "enum": list(MEMBER_CORNER_TREATMENTS), "default": "miter", "description": "How connected segments meet."},
        "connected_segments": {"type": "string", "enum": sorted(CONNECTED_SEGMENTS), "default": "simple_cut"},
        "allow_protrusion": {"type": "boolean", "default": False},
        "angle_deg": {"type": "number", "default": 0, "description": "Rotate the profile about the path."},
        "mirror_profile": {"type": "boolean", "default": False},
        "gap_mm": {"type": "number", "default": 0, "minimum": 0, "description": "Gap between the segments of the group."},
        "name": {"type": "string"},
    },
    ["selection"],
)
def weldment_structural_member(args: dict[str, Any]) -> dict[str, Any]:
    app, doc = require_part()
    exit_active_sketch(doc)
    profile = _resolve_profile(app, args)
    segments = _segment_objects(doc, args.get("selection"))
    if str(args.get("corner_treatment", "miter")) not in MEMBER_CORNER_TREATMENTS:
        return result(False, f"corner_treatment must be one of {', '.join(MEMBER_CORNER_TREATMENTS)}; trimming is weldment_trim_extend.")
    weldment = _ensure_weldment(doc)
    added_weldment = weldment is not None
    try:
        feature, before_bodies = _insert_member_group(doc, profile, segments, args)
    except Exception:
        if added_weldment:
            _remove_weldment(doc, weldment)
        raise
    rename_feature(feature, args.get("name"))
    payload = feature_result(doc, feature, "structural member", profile=str(profile), segments=len(segments))
    if feature is None:
        payload["message"] += (
            " Segments of one call must be connected end to end or parallel; the profile must exist; "
            "a segment that already carries a member of this profile is refused."
        )
        if added_weldment:
            payload["data"]["weldment_removed"] = _remove_weldment(doc, weldment)
        return payload
    bodies = [b for b in bodies_summary(doc) if b["name"] not in before_bodies]
    payload["data"]["bodies"] = bodies
    payload["data"]["volume_mm3"] = volume_total_mm3(bodies)
    payload["data"]["weldment_added"] = added_weldment
    if len(bodies) != len(segments):
        payload["ok"] = False
        payload["message"] += f" Expected {len(segments)} new bodies and found {len(bodies)}."
    return payload


def _insert_member_group(doc: Any, profile: Path, segments: list[Any], args: dict[str, Any]) -> tuple[Any, set[str]]:
    manager = feature_manager(doc)
    group = manager.CreateStructuralMemberGroup()
    group.Segments = dispatch_array(segments)
    treatment = str(args.get("corner_treatment", "miter"))
    group.ApplyCornerTreatment = treatment != "none"
    if treatment != "none":
        group.CornerTreatmentType = CORNER_TREATMENTS[treatment]
    angle = float(args.get("angle_deg", 0))
    if angle:
        group.Angle = to_rad(angle)
    if bool(args.get("mirror_profile", False)):
        group.MirrorProfile = True
    gap = float(args.get("gap_mm", 0))
    if gap:
        group.GapWithinGroup = to_m(gap)

    before_bodies = {str(safe(b, "Name", "")) for b in get_bodies(doc)}
    clear_selection(doc)
    feature = manager.InsertStructuralWeldment4(
        str(profile), CONNECTED_SEGMENTS[str(args.get("connected_segments", "simple_cut"))],
        bool(args.get("allow_protrusion", False)), dispatch_array([group]),
    )
    return feature, before_bodies


@tool(
    "weldment_end_cap",
    "Close the open ends of structural members with a plate. Put the planar end faces in selection.faces "
    "(the ring-shaped end face of a tube, from list_faces). thickness_mm is the plate thickness; the plate is "
    "inset from the outer profile by inset_ratio times the wall thickness, or by inset_mm when given.",
    {
        "selection": SELECTION_SCHEMA,
        "thickness_mm": {"type": "number", "exclusiveMinimum": 0},
        "inset_ratio": {"type": "number", "default": 0.5, "minimum": 0, "description": "Inset as a ratio of the wall thickness."},
        "inset_mm": {"type": "number", "minimum": 0, "description": "Inset as a distance instead of a ratio."},
        "chamfer_mm": {"type": "number", "minimum": 0, "description": "Chamfer the plate corners by this distance."},
        "inward": {"type": "boolean", "default": False, "description": "Sink the plate into the member instead of adding it beyond the end."},
        "reverse": {"type": "boolean", "default": False},
        "name": {"type": "string"},
    },
    ["selection", "thickness_mm"],
)
def weldment_end_cap(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    count = require_selection(doc, args["selection"])
    inset_mm = args.get("inset_mm")
    chamfer = args.get("chamfer_mm")
    before_bodies = {str(safe(b, "Name", "")) for b in get_bodies(doc)}
    # BIsChamfer picks chamfer over fillet; BIsCornerTreatment switches the
    # corner treatment on.  The API help calls corner treatment invalid with
    # a given offset, but on 2016 SP3 inset_mm 1 with chamfer_mm 2 builds a
    # chamfered 18 x 18 x 3 plate of 948 mm³ (measured 2026-10-08).
    feature = feature_manager(doc).InsertEndCapFeature3(
        to_m(args["thickness_mm"]), inset_mm is not None, chamfer is not None,
        to_m(inset_mm) if inset_mm is not None else 0.0, float(args.get("inset_ratio", 0.5)),
        to_m(chamfer) if chamfer is not None else 0.0,
        chamfer is not None, 0.0, bool(args.get("reverse", False)), bool(args.get("inward", False)),
    )
    rename_feature(feature, args.get("name"))
    payload = feature_result(doc, feature, "end cap", faces=count, thickness_mm=args["thickness_mm"])
    if feature is None:
        payload["message"] += " Select the planar end face of a structural member."
        return payload
    bodies = [b for b in bodies_summary(doc) if b["name"] not in before_bodies]
    payload["data"]["bodies"] = bodies
    payload["data"]["volume_mm3"] = volume_total_mm3(bodies)
    definition = safe(feature, "GetDefinition")
    if definition is not None:
        payload["data"]["end_cap"] = {
            "thickness_mm": round(to_mm(safe(definition, "Thickness", 0.0) or 0.0), 6),
            "inset_ratio": safe(definition, "ThicknessRatioForOffset"),
            "inset_mm": round(to_mm(safe(definition, "OffsetDistance", 0.0) or 0.0), 6),
            "chamfered": bool(safe(definition, "UseChamferCorners", False)),
            "inward": bool(safe(definition, "IsEndCapInward", False)),
        }
    return payload


@tool(
    "weldment_trim_extend",
    "Trim (or extend) structural members against other bodies, faces or reference planes, so members that "
    "run into each other end flush. bodies are the indices from list_bodies of the members to trim; "
    "trimming_bodies are the indices of the bodies they stop at, or trimming_selection holds the faces or "
    "planes (front/top/right or a created plane) they stop at. With a body boundary the trimming member grows "
    "over the end of the trimmed one, so the part volume stays the same; check the per-body volumes and boxes "
    "reported in bodies and trimmed_bodies, not the part total.",
    {
        "bodies": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
        "trimming_bodies": {"type": "array", "items": {"type": "integer"}},
        "trimming_selection": SELECTION_SCHEMA,  # faces and planes are what a trim boundary can be
        "corner_type": {
            "type": "string", "enum": ["butt1", "butt2", "miter", "trim"],
            "description": "How the member ends: butt1, butt2 or miter against a body; trim cuts it at a face or plane "
                           "and keeps both pieces. Defaults to butt1 with trimming_bodies and to trim otherwise. "
                           "Only trim takes several bodies or several boundaries in one call.",
        },
        "coped_cut": {"type": "boolean", "default": False},
        "gap_mm": {"type": "number", "minimum": 0, "default": 0, "description": "Weld gap left after trimming."},
        "name": {"type": "string"},
    },
    ["bodies"],
)
def weldment_trim_extend(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    to_trim = _body_objects(doc, list(args["bodies"]))
    trimming: list[Any] = []
    if args.get("trimming_bodies"):
        trimming.extend(_body_objects(doc, list(args["trimming_bodies"])))
    if args.get("trimming_selection"):
        # Whatever the selection resolves to -- faces, reference planes,
        # bodies -- is read back from the selection set, so every kind the
        # schema offers reaches SOLIDWORKS.
        require_selection(doc, args["trimming_selection"])
        trimming.extend(selected_objects(doc))
    if not trimming:
        return result(False, "Give trimming_bodies or trimming_selection (faces or planes): something for the members to stop at.")
    corner_type = str(args.get("corner_type") or ("butt1" if args.get("trimming_bodies") else "trim"))
    count_error = trim_count_error(corner_type, len(to_trim), len(trimming))
    if count_error:
        return result(False, count_error)
    # The two extension bits are set as the dialog does, and they change
    # nothing on 2016 either way: with butt1 the trimming member always grows
    # over the end of the trimmed one (measured with 0, 1, 2 and 3), so the
    # tool does not offer a switch it cannot honour.
    options = TRIM_ALLOW_TRIMMED_EXTENSION | TRIM_ALLOW_TRIMMING_EXTENSION
    if bool(args.get("coped_cut", False)):
        options |= TRIM_COPED_CUT
    # The weld-gap bit is always set: without it SOLIDWORKS takes the last
    # gap from the dialog instead of the value passed here, zero included.
    options |= TRIM_WELD_GAP
    gap = float(args.get("gap_mm", 0))
    # A body boundary wants butt or miter; a face or plane boundary wants
    # trim, which the butt and miter types silently leave uncut (measured:
    # butt1 against a plane builds a feature that changes nothing).
    before = {b["name"]: b for b in bodies_summary(doc)}
    clear_selection(doc)
    feature = feature_manager(doc).InsertWeldmentTrimFeature2(
        CORNER_TREATMENTS[corner_type], options, to_m(gap), dispatch_array(to_trim), dispatch_array(trimming),
    )
    rename_feature(feature, args.get("name"))
    payload = feature_result(doc, feature, "trim/extend", trimmed=len(to_trim), against=len(trimming), corner_type=corner_type)
    if feature is None:
        payload["message"] += " The trimming bodies must actually cross or face the members being trimmed."
        return payload
    # Trimming renames the bodies after the feature; report every body so the
    # caller sees the new boxes, and flag the ones that came from this feature.
    feature_name = str(feature_property(feature, "Name", ""))
    bodies = bodies_summary(doc)
    payload["data"]["bodies"] = bodies
    payload["data"]["trimmed_bodies"] = [b for b in bodies if b["name"].startswith(feature_name)]
    payload["data"]["bodies_before"] = list(before)
    if bodies_unchanged(before, bodies):
        payload["ok"] = False
        payload["message"] += (
            f" No body changed: with corner_type {corner_type} nothing was trimmed. A face or plane boundary "
            "needs corner_type trim; a body boundary must cross the member."
        )
    return payload


@tool(
    "weldment_gusset",
    "Add a gusset plate into the corner between two planar faces of structural members. Put the two "
    "supporting faces in selection.faces (the faces that form the inner corner, from list_faces). A triangle "
    "profile has legs d1_mm along the first face and d2_mm along the second; a polygon profile cuts the "
    "outer corner off: d3_mm rises from the end of d1 and either angle_deg or d4_mm (from the end of d2) "
    "closes it. thickness_mm grows inner, outer or to both sides of the profile plane, which sits at the "
    "start, centre or end of the corner edge. The new body is reported with its volume.",
    {
        "selection": SELECTION_SCHEMA,
        "profile": {"type": "string", "enum": sorted(GUSSET_PROFILES), "default": "triangle"},
        "d1_mm": {"type": "number", "exclusiveMinimum": 0},
        "d2_mm": {"type": "number", "exclusiveMinimum": 0},
        "d3_mm": {"type": "number", "exclusiveMinimum": 0, "description": "Polygon only."},
        "d4_mm": {"type": "number", "exclusiveMinimum": 0, "description": "Polygon only; used instead of angle_deg when given."},
        "angle_deg": {"type": "number", "default": 45, "description": "Polygon only: angle of the cut-off edge."},
        "thickness_mm": {"type": "number", "exclusiveMinimum": 0},
        "thickness_direction": {"type": "string", "enum": sorted(GUSSET_THICKNESS_DIRECTIONS), "default": "both_sides"},
        "location": {"type": "string", "enum": sorted(GUSSET_LOCATIONS), "default": "center"},
        "swap_legs": {"type": "boolean", "default": False, "description": "Swap d1 with d2 (and d3 with d4)."},
        "name": {"type": "string"},
    },
    ["selection", "d1_mm", "d2_mm", "thickness_mm"],
)
def weldment_gusset(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    profile = str(args.get("profile", "triangle"))
    polygon = GUSSET_PROFILES[profile]
    d3 = args.get("d3_mm")
    d4 = args.get("d4_mm")
    if polygon and not d3:
        return result(False, "A polygon gusset needs d3_mm (and angle_deg or d4_mm).")
    # InsertGussetFeature3 reads its two supporting faces from mark 1.
    count = require_selection(doc, args["selection"], mark=1)
    if count != 2:
        return result(False, f"A gusset needs exactly two supporting faces; the selection resolved to {count}.")
    before_bodies = {str(safe(b, "Name", "")) for b in get_bodies(doc)}
    use_d4 = polygon and d4 is not None
    feature = feature_manager(doc).InsertGussetFeature3(
        to_m(args["thickness_mm"]),
        GUSSET_THICKNESS_DIRECTIONS[str(args.get("thickness_direction", "both_sides"))],
        GUSSET_LOCATIONS[str(args.get("location", "center"))],
        polygon, to_m(args["d1_mm"]), to_m(args["d2_mm"]),
        to_m(d3) if polygon else 0.0,
        to_rad(args.get("angle_deg", 45)) if polygon and not use_d4 else 0.0,
        to_m(d4) if use_d4 else 0.0,
        False, 0.0, 0, False, bool(args.get("swap_legs", False)), use_d4,
        0.0, 0.0, 0.0, False, False,
    )
    rename_feature(feature, args.get("name"))
    payload = feature_result(doc, feature, "gusset", profile=profile, thickness_mm=args["thickness_mm"])
    if feature is None:
        payload["message"] += " The two faces must meet along one straight corner edge of the weldment."
        return payload
    bodies = [b for b in bodies_summary(doc) if b["name"] not in before_bodies]
    payload["data"]["bodies"] = bodies
    payload["data"]["volume_mm3"] = volume_total_mm3(bodies)
    definition = safe(feature, "GetDefinition")
    if definition is not None:
        payload["data"]["gusset"] = {
            "thickness_mm": round(to_mm(safe(definition, "Thickness", 0.0) or 0.0), 6),
            "d1_mm": round(to_mm(safe(definition, "ProfileDistance1", 0.0) or 0.0), 6),
            "d2_mm": round(to_mm(safe(definition, "ProfileDistance2", 0.0) or 0.0), 6),
            "d3_mm": round(to_mm(safe(definition, "ProfileDistance3", 0.0) or 0.0), 6),
            "profile": "polygon" if int(safe(definition, "ProfileType", 1) or 0) == 0 else "triangle",
        }
    if not bodies:
        payload["ok"] = False
        payload["message"] += " No new body appeared."
    return payload
