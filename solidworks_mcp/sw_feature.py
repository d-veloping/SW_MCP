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

"""Solid features: extrude, revolve, fillet, chamfer, shell, sweep, loft, rib,
draft, holes, patterns, and mirror.

Every feature that needs topology takes the same declarative ``selection``
object, so the agent never has to reason about SOLIDWORKS' implicit global
selection set or about which mark a given API expects.
"""

from __future__ import annotations

import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from .sw_core import (
    feature_names,
    active_document,
    apply_selection,
    apply_transform,
    as_list,
    body_volume_mm3,
    call_versioned,
    CHAMFER_ANGLE_DISTANCE,
    CHAMFER_DISTANCE_DISTANCE,
    CHAMFER_EQUAL_DISTANCE,
    CHAMFER_TANGENT_PROPAGATION,
    clear_selection,
    delete_features,
    empty_variant,
    END_CONDITIONS,
    exit_active_sketch,
    extension,
    feature_manager,
    feature_property,
    feature_result,
    features_added,
    FILLET_PROPAGATE,
    FILLET_TYPE_SIMPLE,
    FILLET_UNIFORM_RADIUS,
    find_feature,
    flag_methods,
    iter_body_context,
    iter_face_objects,
    iter_feature_objects,
    logger,
    nothing,
    open_sketch_name,
    read_member,
    rebuild,
    rename_feature,
    require_part,
    require_selection,
    result,
    safe,
    select_at_point,
    select_object,
    selectable,
    selected_objects,
    SELECTION_SCHEMA,
    select_sketch_for_feature,
    sketch_manager,
    THIN_SIDES,
    tool,
    ToolFailure,
    to_deg,
    to_m,
    to_mm,
    to_rad,
    value,
    whats_wrong,
)


def _feature_created_after(doc: Any, before: list[str]) -> Any | None:
    """Recover the new feature for the SOLIDWORKS APIs that return void."""
    previous = set(before)
    for feature in reversed(iter_feature_objects(doc)):
        name = str(feature_property(feature, "Name", ""))
        if name and name not in previous:
            return feature
    return None


_END_CONDITION_SCHEMA = {
    "type": "string",
    "enum": sorted(END_CONDITIONS),
    "default": "blind",
    "description": "End condition. blind uses depth_mm; through_all/through_next ignore it.",
}

_SKETCH_ARG = {
    "type": "string",
    "description": "Which sketch to use as the profile. Defaults to the most recently created sketch.",
}


def _two_direction_end_conditions(condition: int) -> tuple[bool, int, int]:
    """Translate the public ``through_all_both`` end condition for feature APIs.

    ``FeatureExtrusion3`` and ``FeatureCut4`` do not honour
    ``swEndCondThroughAllBoth`` when it is supplied as the first end condition
    of a single-ended feature.  They create only the forward half of the
    feature, without a rebuild error.  The API represents the intended result
    as a two-direction feature with ``through all`` on both sides instead.
    """
    if condition == END_CONDITIONS["through_all_both"]:
        through_all = END_CONDITIONS["through_all"]
        return False, through_all, through_all
    return True, condition, 0


_THIN_SCHEMA = {
    "thin_mm": {
        "type": "number",
        "exclusiveMinimum": 0,
        "description": "Wall thickness in millimetres. Makes a thin feature: a wall of constant thickness along the "
        "sketch contour, which may be open (an L, a U, a line) or closed (a tube). Use it instead of a solid "
        "feature followed by a cut of the same contour offset by the wall thickness.",
    },
    "thin_side": {
        "type": "string",
        "enum": sorted(THIN_SIDES),
        "default": "one_side",
        "description": "Where the wall grows from the contour: one_side, reverse (the other side), mid_plane "
        "(half the thickness on each side), or two_side (thin_mm on one side, thin_mm_2 on the other).",
    },
    "thin_mm_2": {
        "type": "number",
        "exclusiveMinimum": 0,
        "description": "Second wall thickness in millimetres, only with thin_side two_side.",
    },
}


def _number(args: dict[str, Any], key: str) -> float:
    raw = args[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw <= 0:
        raise ToolFailure(f"{key} must be a finite number greater than 0, got {raw!r}.")
    return float(raw)


def _thin_args(args: dict[str, Any]) -> tuple[int, float, float] | None:
    """The thin-wall type and both thicknesses in metres, or None for a solid feature.

    Checks every thin argument before the handler touches SOLIDWORKS: a thin side or a second thickness without
    thin_mm, a second thickness without two_side, two_side without one, and a draft on a thin extrusion are refused.
    """
    has_thin = args.get("thin_mm") is not None
    side_given = args.get("thin_side") is not None
    second_given = args.get("thin_mm_2") is not None
    if not has_thin:
        if side_given or second_given:
            raise ToolFailure("thin_side and thin_mm_2 need thin_mm.")
        return None
    thickness = _number(args, "thin_mm")
    side = str(args.get("thin_side") or "one_side")
    if side not in THIN_SIDES:
        raise ToolFailure(f"thin_side must be one of {', '.join(sorted(THIN_SIDES))}, got {side!r}.")
    second = 0.0
    if side == "two_side":
        if not second_given:
            raise ToolFailure("thin_side two_side needs thin_mm_2.")
        second = _number(args, "thin_mm_2")
    elif second_given:
        raise ToolFailure("thin_mm_2 only applies to thin_side two_side.")
    if abs(float(args.get("draft_deg", 0) or 0)) > 1e-12:
        raise ToolFailure("A thin feature takes no draft_deg.")
    return THIN_SIDES[side], to_m(thickness), to_m(second)


def _thin_data(args: dict[str, Any], thin: tuple[int, float, float] | None) -> dict[str, Any]:
    """The thin arguments as the answer reports them; empty for a solid feature."""
    if thin is None:
        return {}
    data: dict[str, Any] = {"thin_mm": args["thin_mm"], "thin_side": str(args.get("thin_side") or "one_side")}
    if args.get("thin_mm_2") is not None:
        data["thin_mm_2"] = args["thin_mm_2"]
    return data


# --------------------------------------------------------------------------
# Extrude and cut
# --------------------------------------------------------------------------


@tool(
    "boss_extrude",
    "Extrude a sketch into a solid boss. Depth is millimetres, draft is degrees. With thin_mm the boss is a "
    "thin feature: a wall of constant thickness along an open or closed contour, in one feature instead of a "
    "solid boss plus a cut of the offset contour.",
    {
        "depth_mm": {"type": "number", "exclusiveMinimum": 0, "default": 10},
        "sketch_name": _SKETCH_ARG,
        "end_condition": _END_CONDITION_SCHEMA,
        "reverse": {"type": "boolean", "default": False, "description": "Extrude the other way."},
        "draft_deg": {"type": "number", "default": 0},
        "draft_outward": {"type": "boolean", "default": False},
        "merge": {"type": "boolean", "default": True, "description": "Merge with existing solid bodies."},
        "name": {"type": "string", "description": "Rename the resulting feature."},
        **_THIN_SCHEMA,
    },
)
def boss_extrude(args: dict[str, Any]) -> dict[str, Any]:
    thin = _thin_args(args)
    _, doc = require_part()
    sketch = select_sketch_for_feature(doc, args.get("sketch_name"))
    condition = END_CONDITIONS[str(args.get("end_condition", "blind"))]
    single_direction, condition_1, condition_2 = _two_direction_end_conditions(condition)
    depth = to_m(args.get("depth_mm", 10))
    draft = to_rad(args.get("draft_deg", 0))
    has_draft = abs(draft) > 1e-12

    if thin is None:
        feature = feature_manager(doc).FeatureExtrusion3(
            single_direction, False, bool(args.get("reverse", False)),
            condition_1, condition_2, depth, depth,
            has_draft, False, bool(args.get("draft_outward", False)), False,
            draft, 0.0,
            False, False, False, False,
            bool(args.get("merge", True)), True, True,
            0, 0.0, False,
        )
    else:
        thin_type, thickness_1, thickness_2 = thin
        # After Merge: Thk1, Thk2, EndThk, RevThinDir (swThinWallType_e), CapEnds, AddBends, BendRad.
        feature = feature_manager(doc).FeatureExtrusionThin2(
            single_direction, False, bool(args.get("reverse", False)),
            condition_1, condition_2, depth, depth,
            False, False, False, False,
            0.0, 0.0,
            False, False, False, False,
            bool(args.get("merge", True)),
            thickness_1, thickness_2, 0.0, thin_type, False, False, 0.0,
            True, True,
            0, 0.0, False,
        )
    rename_feature(feature, args.get("name"))
    action = "thin boss extrusion" if thin else "boss extrusion"
    return feature_result(
        doc, feature, action, sketch=sketch, depth_mm=args.get("depth_mm", 10), **_thin_data(args, thin)
    )


@tool(
    "cut_extrude",
    "Cut material using a sketch profile. Depth is millimetres, draft is degrees. With thin_mm the cut is a "
    "thin feature: it removes a wall of constant thickness along an open or closed contour, such as a groove.",
    {
        "depth_mm": {"type": "number", "exclusiveMinimum": 0, "default": 10},
        "sketch_name": _SKETCH_ARG,
        "end_condition": _END_CONDITION_SCHEMA,
        "reverse": {"type": "boolean", "default": False},
        "flip_side": {"type": "boolean", "default": False, "description": "Cut everything outside the profile instead."},
        "draft_deg": {"type": "number", "default": 0},
        "name": {"type": "string"},
        **_THIN_SCHEMA,
    },
)
def cut_extrude(args: dict[str, Any]) -> dict[str, Any]:
    thin = _thin_args(args)
    _, doc = require_part()
    condition = END_CONDITIONS[str(args.get("end_condition", "blind"))]
    single_direction, condition_1, condition_2 = _two_direction_end_conditions(condition)
    depth = to_m(args.get("depth_mm", 10))
    draft = to_rad(args.get("draft_deg", 0))
    has_draft = abs(draft) > 1e-12
    requested = bool(args.get("reverse", False))

    def attempt(reverse: bool) -> tuple[str, Any]:
        name = select_sketch_for_feature(doc, args.get("sketch_name"))
        if thin is not None:
            thin_type, thickness_1, thickness_2 = thin
            thin_args = (
                single_direction, bool(args.get("flip_side", False)), reverse,
                condition_1, condition_2, depth, depth,
                False, False, False, False,
                0.0, 0.0,
                False, False, False, False,
                thickness_1, thickness_2, 0.0, thin_type, False, False, 0.0,
                True, True,
                0, 0.0, False,
            )
            # FeatureCutThin (2014-2016 builds) takes the same list without the start-condition triple.
            return name, call_versioned(
                feature_manager(doc),
                ("FeatureCutThin2", thin_args),
                ("FeatureCutThin", thin_args[:26]),
            )
        cut_args = (
            single_direction, bool(args.get("flip_side", False)), reverse,
            condition_1, condition_2, depth, depth,
            has_draft, False, False, False,
            draft, 0.0,
            False, False, False, False,
            False, True, True,
            False, False, False,
            0, 0.0, False, False,
        )
        # FeatureCut3 (2014-2016 builds) takes the same list without the
        # trailing OptimizeGeometry flag.
        return name, call_versioned(
            feature_manager(doc),
            ("FeatureCut4", cut_args),
            ("FeatureCut3", cut_args[:26]),
        )

    sketch, feature = attempt(requested)
    reversed_automatically = False
    if feature is None:
        # Which way "forward" points depends on the sketch plane's orientation,
        # and a sketch lying on a boundary face of the body cuts into thin air.
        # A cut that removes nothing is unambiguous, so try the other way.
        sketch, feature = attempt(not requested)
        reversed_automatically = feature is not None

    rename_feature(feature, args.get("name"))
    payload = feature_result(
        doc, feature, "thin cut" if thin else "cut", sketch=sketch, depth_mm=args.get("depth_mm", 10),
        **_thin_data(args, thin),
    )
    if reversed_automatically:
        payload["data"]["reversed_automatically"] = True
        payload["message"] += " The requested direction removed nothing, so it was cut the other way."
    elif feature is None:
        payload["message"] += (
            " Neither direction removed material: check that the profile actually overlaps the body."
        )
    return payload


@tool(
    "revolve",
    "Revolve a sketch profile about an axis. If the sketch contains exactly one centerline "
    "SOLIDWORKS uses it automatically; otherwise pass axis_selection (a sketch_segments index for "
    "the centerline, or an axes name). Angle is degrees. With thin_mm the revolve is a thin feature: a wall of "
    "constant thickness along an open or closed profile, such as a sleeve from a single line.",
    {
        "angle_deg": {"type": "number", "default": 360, "exclusiveMinimum": 0, "maximum": 360},
        "sketch_name": _SKETCH_ARG,
        "axis_selection": SELECTION_SCHEMA,
        "cut": {"type": "boolean", "default": False, "description": "Remove material instead of adding it."},
        "reverse": {"type": "boolean", "default": False},
        "mid_plane": {"type": "boolean", "default": False, "description": "Revolve symmetrically about the sketch plane."},
        "merge": {"type": "boolean", "default": True},
        "name": {"type": "string"},
        **_THIN_SCHEMA,
    },
)
def revolve(args: dict[str, Any]) -> dict[str, Any]:
    thin = _thin_args(args)
    _, doc = require_part()
    sketch = select_sketch_for_feature(doc, args.get("sketch_name"))
    axis_spec = args.get("axis_selection")
    if axis_spec:
        # Mark 4 is what FeatureRevolve2 reads the axis from; append so the
        # profile selection made above survives.
        require_selection(doc, axis_spec, mark=4, append=True)

    angle = to_rad(args.get("angle_deg", 360))
    mid_plane = bool(args.get("mid_plane", False))
    single_direction = not mid_plane
    direction_type = 4 if mid_plane else 0  # swRevolveType_e: 4 = mid plane

    thin_type, thickness_1, thickness_2 = thin if thin is not None else (0, 0.0, 0.0)
    # IsSolid stays true for a thin revolve; IsThin, ThinType and the two thicknesses make the wall.
    feature = feature_manager(doc).FeatureRevolve2(
        single_direction, True, thin is not None, bool(args.get("cut", False)),
        bool(args.get("reverse", False)), False,
        direction_type, 0, angle, 0.0,
        False, False, 0.0, 0.0,
        thin_type, thickness_1, thickness_2,
        bool(args.get("merge", True)), True, True,
    )
    rename_feature(feature, args.get("name"))
    return feature_result(
        doc, feature, "thin revolve" if thin else "revolve", sketch=sketch, angle_deg=args.get("angle_deg", 360),
        **_thin_data(args, thin),
    )


# --------------------------------------------------------------------------
# Dress-up features
# --------------------------------------------------------------------------


@tool(
    "fillet",
    "Round the selected edges or faces with a constant radius, in millimetres. "
    "Use list_edges to find the edge indices, or selection.face_edges to fillet every edge of a face.",
    {
        "radius_mm": {"type": "number", "exclusiveMinimum": 0},
        "selection": SELECTION_SCHEMA,
        "propagate": {"type": "boolean", "default": True, "description": "Continue across tangent faces."},
        "keep_features": {"type": "boolean", "default": False},
        "name": {"type": "string"},
    },
    ["radius_mm", "selection"],
)
def fillet(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    count = require_selection(doc, args["selection"])
    options = FILLET_UNIFORM_RADIUS
    if bool(args.get("propagate", True)):
        options |= FILLET_PROPAGATE

    feature = feature_manager(doc).FeatureFillet3(
        options, to_m(args["radius_mm"]), 0.0, 0.0,
        FILLET_TYPE_SIMPLE, 0, 0,
        empty_variant(), empty_variant(), empty_variant(),
        empty_variant(), empty_variant(), empty_variant(), empty_variant(),
    )
    rename_feature(feature, args.get("name"))
    return feature_result(doc, feature, "fillet", radius_mm=args["radius_mm"], entities=count)


@tool(
    "chamfer",
    "Chamfer the selected edges or faces. equal_distance uses distance_mm on both sides; "
    "angle_distance uses distance_mm and angle_deg; distance_distance uses both distances.",
    {
        "distance_mm": {"type": "number", "exclusiveMinimum": 0},
        "selection": SELECTION_SCHEMA,
        "mode": {
            "type": "string",
            "enum": ["equal_distance", "angle_distance", "distance_distance"],
            "default": "equal_distance",
        },
        "angle_deg": {"type": "number", "default": 45},
        "other_distance_mm": {"type": "number", "description": "Second distance for distance_distance."},
        "propagate": {"type": "boolean", "default": True},
        "flip": {"type": "boolean", "default": False},
        "name": {"type": "string"},
    },
    ["distance_mm", "selection"],
)
def chamfer(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    count = require_selection(doc, args["selection"])
    mode = str(args.get("mode", "equal_distance"))
    # swChamferEqualDistance (16) is accepted by InsertFeatureChamfer on 2016
    # and creates a feature that removes nothing -- measured: 0 mm³ instead of
    # 270 on four 15 mm edges.  Two equal distances are the same geometry and
    # work on every build, so that is what equal_distance sends.
    chamfer_type = {
        "equal_distance": CHAMFER_DISTANCE_DISTANCE,
        "angle_distance": CHAMFER_ANGLE_DISTANCE,
        "distance_distance": CHAMFER_DISTANCE_DISTANCE,
    }[mode]
    options = CHAMFER_TANGENT_PROPAGATION if bool(args.get("propagate", True)) else 0
    if bool(args.get("flip", False)):
        options |= 1  # swFeatureChamferFlipDirection
    other = to_m(args["distance_mm"]) if mode == "equal_distance" else to_m(args.get("other_distance_mm", args["distance_mm"]))

    feature = feature_manager(doc).InsertFeatureChamfer(
        options, chamfer_type, to_m(args["distance_mm"]),
        to_rad(args.get("angle_deg", 45)), other,
        0.0, 0.0, 0.0,
    )
    rename_feature(feature, args.get("name"))
    return feature_result(doc, feature, "chamfer", distance_mm=args["distance_mm"], entities=count)


@tool(
    "shell",
    "Hollow the part to a wall thickness in millimetres. Pass selection with the faces to remove; "
    "omit it to hollow the body with no opening.",
    {
        "thickness_mm": {"type": "number", "exclusiveMinimum": 0},
        "selection": SELECTION_SCHEMA,
        "outward": {"type": "boolean", "default": False, "description": "Thicken outward instead of inward."},
        "name": {"type": "string"},
    },
    ["thickness_mm"],
)
def shell(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    before = feature_names(doc)
    removed = 0
    if args.get("selection"):
        removed = apply_selection(doc, args["selection"])
    else:
        clear_selection(doc)

    doc.InsertFeatureShell(to_m(args["thickness_mm"]), bool(args.get("outward", False)))
    feature = _feature_created_after(doc, before)
    rename_feature(feature, args.get("name"))
    return feature_result(doc, feature, "shell", thickness_mm=args["thickness_mm"], removed_faces=removed)


@tool(
    "draft",
    "Apply draft to the selected faces about a neutral plane or face. Angle is degrees. "
    "Put the faces to draft in selection and the neutral reference in neutral_selection.",
    {
        "angle_deg": {"type": "number", "exclusiveMinimum": 0, "default": 3},
        "selection": SELECTION_SCHEMA,
        "neutral_selection": SELECTION_SCHEMA,
        "reverse": {
            "type": "boolean",
            "default": False,
            "description": "False tapers the face outward, adding material below the neutral plane; "
                           "True tapers it inward and removes material. For a moulded part you "
                           "normally want True.",
        },
        "name": {"type": "string"},
    },
    ["angle_deg", "selection", "neutral_selection"],
)
def draft(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    # Mark 1 is the neutral plane, mark 2 the faces being drafted.
    require_selection(doc, args["neutral_selection"], mark=1)
    faces = require_selection(doc, args["selection"], mark=2, append=True)

    feature = feature_manager(doc).InsertMultiFaceDraft(
        to_rad(args["angle_deg"]), bool(args.get("reverse", False)), False, 0, False, False
    )
    rename_feature(feature, args.get("name"))
    return feature_result(doc, feature, "draft", angle_deg=args["angle_deg"], faces=faces)


@tool(
    "rib",
    "Turn an open sketch profile into a rib of the given thickness in millimetres. The usual setup "
    "is a single line drawn on the cross-section plane, bridging two existing faces — the profile "
    "must reach material at both ends or SOLIDWORKS creates nothing.",
    {
        "thickness_mm": {"type": "number", "exclusiveMinimum": 0},
        "sketch_name": _SKETCH_ARG,
        "two_sided": {"type": "boolean", "default": True, "description": "Thicken symmetrically about the sketch."},
        "reverse_material": {"type": "boolean", "default": False, "description": "Flip which way the rib grows."},
        "extrude_direction": {
            "type": "string",
            "enum": ["parallel_to_sketch", "normal_to_sketch"],
            "default": "parallel_to_sketch",
            "description": "How the profile reaches the material. A cross-section profile wants "
                           "parallel_to_sketch; the other is tried automatically if this one builds nothing.",
        },
        "draft_deg": {"type": "number", "default": 0},
        "name": {"type": "string"},
    },
    ["thickness_mm"],
)
def rib(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    draft_deg = float(args.get("draft_deg", 0))
    requested = str(args.get("extrude_direction", "parallel_to_sketch")) == "normal_to_sketch"

    def attempt(normal_to_sketch: bool) -> tuple[str, Any]:
        name = select_sketch_for_feature(doc, args.get("sketch_name"))
        before = feature_names(doc)
        feature_manager(doc).InsertRib(
            bool(args.get("two_sided", True)), False, to_m(args["thickness_mm"]), 0,
            bool(args.get("reverse_material", False)),
            abs(draft_deg) > 1e-12, False, to_rad(draft_deg),
            normal_to_sketch, False,
        )
        return name, _feature_created_after(doc, before)

    sketch, feature = attempt(requested)
    flipped = False
    if feature is None:
        # InsertRib returns void and simply builds nothing when the extrusion
        # direction cannot reach material, so the other one is worth a try.
        sketch, feature = attempt(not requested)
        flipped = feature is not None

    rename_feature(feature, args.get("name"))
    payload = feature_result(doc, feature, "rib", sketch=sketch, thickness_mm=args["thickness_mm"])
    if flipped:
        used = "normal_to_sketch" if not requested else "parallel_to_sketch"
        payload["data"]["extrude_direction"] = used
        payload["message"] += f" The requested direction built nothing, so {used} was used."
    elif feature is None:
        payload["message"] += (
            " Neither extrusion direction reached material: check that the profile spans between "
            "two existing faces."
        )
    return payload


@tool(
    "simple_hole",
    "Cut a straight hole of a given diameter into a face. Put the face and the hole centre in "
    "selection, most simply as a points entry with type FACE at the hole location.",
    {
        "diameter_mm": {"type": "number", "exclusiveMinimum": 0},
        "selection": SELECTION_SCHEMA,
        "depth_mm": {"type": "number", "exclusiveMinimum": 0, "default": 10},
        "end_condition": _END_CONDITION_SCHEMA,
        "reverse": {"type": "boolean", "default": False},
        "name": {"type": "string"},
    },
    ["diameter_mm", "selection"],
)
def simple_hole(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    require_selection(doc, args["selection"])
    condition = END_CONDITIONS[str(args.get("end_condition", "blind"))]
    depth = to_m(args.get("depth_mm", 10))

    feature = feature_manager(doc).SimpleHole2(
        to_m(args["diameter_mm"]), True, False, bool(args.get("reverse", False)),
        condition, 0, depth, 0.0,
        False, False, False, False, 0.0, 0.0,
        False, False, False, False,
        True, True, False, False, False,
    )
    rename_feature(feature, args.get("name"))
    return feature_result(doc, feature, "hole", diameter_mm=args["diameter_mm"])


# --------------------------------------------------------------------------
# Hole Wizard (IFeatureManager.HoleWizard5)
# --------------------------------------------------------------------------

# kind -> (swWzdGeneralHoleTypes_e, the bit the Toolbox database sets in ValidHoleTypes for it)
HOLE_KINDS = {"counterbore": (0, 1), "countersink": (1, 2), "clearance": (2, 4), "tapped": (4, 8)}
# standard -> (swWzdHoleStandards_e, table prefix in the Toolbox database)
HOLE_STANDARDS = {
    "ansi_inch": (0, "AI"), "ansi_metric": (1, "AM"), "bsi": (2, "BSI"), "din": (4, "DIN"), "iso": (8, "ISO"),
    "jis": (9, "JIS"), "gb": (13, "GB"), "ks": (14, "KS"),
}
HOLE_FITS = {"close": 0, "normal": 1, "loose": 2}            # swWzdHoleScrewClearanceTypes_e
HOLE_END_CONDITIONS = {"blind": 0, "through_all": 1}         # swEndConditions_e
SW_HOLE_WIZARD_TOOLBOX_FOLDER = 52                           # swUserPreferenceStringValue_e.swHoleWizardToolBoxFolder
_COSMETIC_THREAD_WITH_CALLOUT, _COSMETIC_THREAD_NONE = 1, 0  # swWzdHoleCosmeticThreadTypes_e
_THREAD_END_BLIND = 0                                        # swWzdHoleThreadEndCondition_e
_IGNORED = -1.0                                              # "SOLIDWORKS ignores parameters set to -1" (API help)
_REMOVED_MIN_MM3 = 1e-3
_POINT_TOLERANCE_MM = 1e-3

# swWzdHoleTypes_e, values 0..79 in order, then 90.
_HOLE_TYPE_NAMES = dict(enumerate((
    "swSimple swTapered swCounterBored swCounterSunk swCounterDrilled swSimpleDrilled swTaperedDrilled "
    "swCounterBoredDrilled swCounterSunkDrilled swCounterDrilledDrilled swCounterBoreBlind "
    "swCounterBoreBlindCounterSinkMiddle swCounterBoreBlindCounterSinkTop swCounterBoreBlindCounterSinkTopmiddle "
    "swCounterBoreThru swCounterBoreThruCounterSinkBottom swCounterBoreThruCounterSinkMiddle "
    "swCounterBoreThruCounterSinkMiddleBottom swCounterBoreThruCounterSinkTop swCounterBoreThruCounterSinkTopBottom "
    "swCounterBoreThruCounterSinkTopMiddle swCounterBoreThruCounterSinkTopMiddleBottom swHoleBlind "
    "swHoleBlindCounterSinkTop swCounterSinkBlind swHoleThru swHoleThruCounterSinkBottom swHoleThruCounterSinkTop "
    "swHoleThruCounterSinkTopBottom swCounterSinkThru swCounterSinkThruCounterSinkBottom swTapBlind "
    "swTapBlindCounterSinkTop swTapThru swTapThruCounterSinkBottom swTapThruCounterSinkTop "
    "swTapThruCounterSinkTopBottom swPipeTapBlind swPipeTapBlindCounterSinkTop swPipeTapThru "
    "swPipeTapThruCounterSinkBottom swPipeTapThruCounterSinkTop swPipeTapThruCounterSinkTopBottom "
    "swCounterSinkBlindWithoutHeadClearance swCounterSinkThruWithoutHeadClearance "
    "swCounterSinkThruCounterSinkBottomWithoutHeadClearance swTapBlindCosmeticThread "
    "swTapBlindCosmeticThreadCounterSinkTop swTapThruCosmeticThread swTapThruCosmeticThreadCounterSinkTop "
    "swTapThruCosmeticThreadCounterSinkBottom swTapThruCosmeticThreadCounterSinkTopBottom swTapThruThreadThru "
    "swTapThruThreadThruCounterSinkTop swTapThruThreadThruCounterSinkBottom swTapThruThreadThruCountersinkTopBottom "
    "swTapBlindRemoveThread swCounterBoreSlotBlind swCounterBoreSlotBlindCounterSinkMiddle "
    "swCounterBoreSlotBlindCounterSinkTop swCounterBoreSlotBlindCounterSinkTopMiddle swCounterBoreSlotThru "
    "swCounterBoreSlotThruCounterSinkBottom swCounterBoreSlotThruCounterSinkMiddle "
    "swCounterBoreSlotThruCounterSinkMiddleBottom swCounterBoreSlotThruCounterSinkTop "
    "swCounterBoreSlotThruCounterSinkTopBottom swCounterBoreSlotThruCounterSinkTopMiddle "
    "swCounterBoreSlotThruCounterSinkTopMiddleBottom swSlotBlind swSlotBlindCounterSinkTop swCounterSinkSlotBlind "
    "swSlotThru swSlotThruCounterSinkBottom swSlotThruCounterSinkTop swSlotThruCounterSinkTopBottom "
    "swCounterSinkSlotThru swCounterSinkSlotThruCounterSinkBottom swCounterSinkSlotBlindWithoutHeadClearance "
    "swCounterSinkSlotThruWithoutHeadClearance"
).split()))
_HOLE_TYPE_NAMES[90] = "swCounterSinkSlotThruCounterSinkBottomWithoutHeadClearance"

# IWizardHoleFeatureData2 fields reported per kind. The values sit in the kind-specific fields; a blind clearance hole,
# counterbore or countersink carries them in HoleDiameter/HoleDepth, a through one in ThruHoleDiameter/ThruHoleDepth
# (measured on 2016 SP3).
_HOLE_FIELDS = {
    "clearance": ("ThruHoleDiameter", "ThruHoleDepth", "HoleDiameter", "HoleDepth", "DrillAngle", "HoleFit"),
    "tapped": ("TapDrillDiameter", "TapDrillDepth", "ThruTapDrillDiameter", "ThruTapDrillDepth", "ThreadDiameter",
               "ThreadDepth", "CosmeticThreadType", "ThreadEndCondition", "DrillAngle"),
    "counterbore": ("ThruHoleDiameter", "ThruHoleDepth", "HoleDiameter", "HoleDepth", "CounterBoreDiameter",
                    "CounterBoreDepth", "HeadClearance", "HoleFit"),
    "countersink": ("ThruHoleDiameter", "ThruHoleDepth", "HoleDiameter", "HoleDepth", "CounterSinkDiameter",
                    "CounterSinkAngle", "HeadClearance", "HeadClearanceType", "HoleFit"),
}
# Where a blind hole reads back its drilled depth (to the drill shoulder), measured on 2016 SP3.
_BLIND_DEPTH_FIELDS = {"clearance": "HoleDepth_mm", "counterbore": "HoleDepth_mm", "countersink": "HoleDepth_mm",
                       "tapped": "TapDrillDepth_mm"}
_HOLE_INTEGER_FIELDS = frozenset({"HoleFit", "CosmeticThreadType", "ThreadEndCondition", "HeadClearanceType"})
_HOLE_ANGLE_FIELDS = frozenset({"DrillAngle", "CounterSinkAngle"})
_SQL_NAME = re.compile(r"^[A-Za-z0-9_]+$")

_POINT_SCHEMA = {
    "type": "object",
    "properties": {"x_mm": {"type": "number"}, "y_mm": {"type": "number"}, "z_mm": {"type": "number"}},
    "required": ["x_mm", "y_mm", "z_mm"],
}


def _hole_wizard_args(args: dict[str, Any]) -> dict[str, Any]:
    """Check every argument before SOLIDWORKS is touched; the checked request, lengths still in millimetres."""
    kind = str(args.get("kind") or "")
    if kind not in HOLE_KINDS:
        raise ToolFailure(f"kind must be one of {', '.join(sorted(HOLE_KINDS))}, got {args.get('kind')!r}.")
    standard = str(args.get("standard") or "").lower()
    if standard not in HOLE_STANDARDS:
        raise ToolFailure(f"standard must be one of {', '.join(sorted(HOLE_STANDARDS))}, got {args.get('standard')!r}.")
    fastener, size = str(args.get("fastener") or "").strip(), str(args.get("size") or "").strip()
    if not fastener or not size:
        raise ToolFailure("fastener and size are required: the Toolbox table name and the size, e.g. 'Screw "
                          "Clearances' and 'M6'.")
    end = str(args.get("end_condition") or "")
    if end not in HOLE_END_CONDITIONS:
        raise ToolFailure(f"end_condition must be blind or through_all, got {args.get('end_condition')!r}.")
    depth = None
    if end == "blind":
        if args.get("depth_mm") is None:
            raise ToolFailure("end_condition blind needs depth_mm.")
        depth = _number(args, "depth_mm")
    elif args.get("depth_mm") is not None:
        raise ToolFailure("depth_mm only applies to end_condition blind.")
    request: dict[str, Any] = {"kind": kind, "standard": standard, "fastener": fastener, "size": size,
                               "end_condition": end, "depth_mm": depth}
    if kind == "tapped":
        if args.get("fit") is not None:
            raise ToolFailure("fit does not apply to a tapped hole.")
        if args.get("thread_depth_mm") is None:
            raise ToolFailure("A tapped hole needs thread_depth_mm.")
        request["thread_depth_mm"] = _number(args, "thread_depth_mm")
        if depth is not None and request["thread_depth_mm"] > depth:
            raise ToolFailure("thread_depth_mm cannot be deeper than the drilled depth_mm.")
        cosmetic = args.get("cosmetic", True)
        if not isinstance(cosmetic, bool):
            raise ToolFailure(f"cosmetic must be true or false, got {cosmetic!r}.")
        request["cosmetic"] = cosmetic
    else:
        if args.get("thread_depth_mm") is not None or args.get("cosmetic") is not None:
            raise ToolFailure("thread_depth_mm and cosmetic only apply to a tapped hole.")
        fit = str(args.get("fit") or "normal")
        if fit not in HOLE_FITS:
            raise ToolFailure(f"fit must be one of {', '.join(sorted(HOLE_FITS))}, got {args.get('fit')!r}.")
        request["fit"] = fit
    has_index, has_point = args.get("face_index") is not None, args.get("face_point_mm") is not None
    if has_index == has_point:
        raise ToolFailure("Pass exactly one of face_index (from list_faces) and face_point_mm.")
    if has_index:
        raw = args["face_index"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ToolFailure(f"face_index must be a non-negative integer, got {raw!r}.")
        request["face_index"] = raw
    else:
        request["face_point_mm"] = _xyz(args["face_point_mm"], "face_point_mm")
    points = args.get("points")
    if not isinstance(points, list) or not points:
        raise ToolFailure("points needs at least one hole centre {x_mm, y_mm, z_mm} in model coordinates.")
    request["points_mm"] = [_xyz(p, f"points[{i}]") for i, p in enumerate(points)]
    if args.get("name") is not None:
        request["name"] = str(args["name"])
    return request


def _xyz(raw: Any, label: str) -> list[float]:
    """A model point in millimetres from {x_mm, y_mm, z_mm} or [x, y, z]."""
    if isinstance(raw, dict):
        raw = [raw.get("x_mm"), raw.get("y_mm"), raw.get("z_mm")]
    if (not isinstance(raw, (list, tuple)) or len(raw) != 3
            or any(isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) for c in raw)):
        raise ToolFailure(f"{label} must be three finite numbers x_mm, y_mm, z_mm, got {raw!r}.")
    return [float(c) for c in raw]


def _toolbox_database(app: Any) -> Path:
    """The Toolbox database the Hole Wizard reads its standards from, swbrowser.sldedb in the folder SOLIDWORKS is
    configured with (Tools > Options > Hole Wizard/Toolbox). 2016 SP3 reports the language folder itself
    (C:/SOLIDWORKS Data/lang/english, measured); a Toolbox root is searched under lang/, English first."""
    try:
        folder = str(app.GetUserPreferenceStringValue(SW_HOLE_WIZARD_TOOLBOX_FOLDER) or "")
    except Exception as exc:
        raise ToolFailure(f"The Hole Wizard/Toolbox folder cannot be read from SOLIDWORKS ({exc}).") from exc
    if not folder:
        raise ToolFailure("SOLIDWORKS has no Hole Wizard/Toolbox folder configured (Tools > Options > Hole "
                          "Wizard/Toolbox); hole_wizard needs its standards database.")
    lang = Path(folder) / "lang"
    candidates = [Path(folder) / "swbrowser.sldedb", lang / "english" / "swbrowser.sldedb",
                  *sorted(lang.glob("*/swbrowser.sldedb"))]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ToolFailure(f"No Toolbox database swbrowser.sldedb in {folder} or under {lang}.", toolbox_folder=folder)


def _toolbox_lookup(database: Path, standard: str, fastener: str, size: str, kind: str) -> int:
    """The swWzdHoleStandardFastenerTypes_e value for this fastener table, after checking that the table exists in
    the Toolbox database, offers this kind of hole and lists this size. Read-only; every refusal names what exists."""
    prefix = HOLE_STANDARDS[standard][1]
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ToolFailure(f"The Toolbox database {database} cannot be opened ({exc}).") from exc
    try:
        try:
            tables = [str(row[0]) for row in connection.execute(
                f"SELECT DISTINCT CategoryID FROM [{prefix}_Types] WHERE HasHoles <> 0")]
        except sqlite3.Error as exc:
            raise ToolFailure(f"The Toolbox database {database} has no hole tables for {standard} ({exc}).") from exc
        known: dict[str, tuple[str, str, int, int]] = {}
        for table in tables:
            if not _SQL_NAME.match(table):
                continue
            try:
                rows = connection.execute(
                    f"SELECT Title, DataTable, SWConst_Enum_Value, ValidHoleTypes FROM [{table}] "
                    "WHERE HasHoles <> 0 AND enabled <> 0").fetchall()
            except sqlite3.Error:
                continue
            for title, data_table, enum_value, valid in rows:
                if title and enum_value is not None:
                    known.setdefault(str(title).casefold(), (str(title), str(data_table or ""), int(enum_value),
                                                             int(valid or 0)))
        entry = known.get(fastener.casefold())
        bit = HOLE_KINDS[kind][1]
        if entry is None:
            offered = sorted(t for t, _, _, v in known.values() if v & bit)
            raise ToolFailure(f"No fastener table '{fastener}' for {standard} in the Toolbox database.",
                              fasteners_for_kind=offered)
        title, data_table, enum_value, valid = entry
        if not valid & bit:
            offered = sorted(t for t, _, _, v in known.values() if v & bit)
            raise ToolFailure(f"'{title}' offers no {kind} hole.", fasteners_for_kind=offered)
        table = f"{prefix}_{data_table[1:]}" if data_table.startswith("+") else data_table
        if not _SQL_NAME.match(table):
            raise ToolFailure(f"'{title}' names no readable size table ({data_table!r}).")
        try:
            columns = {str(row[1]).casefold() for row in connection.execute(f"PRAGMA table_info([{table}])")}
            enabled = " WHERE enabled <> 0" if "enabled" in columns else ""
            sizes = [str(row[0]) for row in connection.execute(f"SELECT SIZE FROM [{table}]{enabled}")]
        except sqlite3.Error as exc:
            raise ToolFailure(f"The sizes of '{title}' cannot be read from {table} ({exc}).") from exc
        if size not in sizes:
            raise ToolFailure(f"Unknown size '{size}' for '{title}' ({standard}).", sizes=sizes)
        return enum_value
    finally:
        connection.close()


def _hole_wizard_call(request: dict[str, Any], fastener_index: int) -> list[Any]:
    """The 27 HoleWizard5 arguments (SOLIDWORKS 2016 SP3 type library), metres and radians.

    Value1..Value12 mean something different per kind (API help, IFeatureManager::HoleWizard5); -1 leaves a value to
    the standard. Measured on 2016 SP3: a clearance hole refuses -1 for Diameter/Depth/Length (None, no exception) and
    takes 0 for the table value; its drill angle must be -1, since 0 builds a blind hole with a 0-degree tip that
    removes far too much without any error. Tapped holes, counterbores and countersinks take -1.
    """
    kind = request["kind"]
    blind = request["end_condition"] == "blind"
    depth = to_m(request["depth_mm"]) if blind else None
    if kind == "clearance":
        # Screw fit, drill angle, near csink dia/angle, far csink dia/angle, offset
        sizes = (0.0, depth if blind else 0.0, 0.0)
        values = [HOLE_FITS[request["fit"]], _IGNORED] + [0.0] * 10
    elif kind == "tapped":
        # Thread depth, near csink dia/angle, far csink dia/angle, drill angle, cosmetic thread, thread end,
        # helicoil tap type, offset; the tap drill diameter and depth are Diameter and Depth.
        sizes = (_IGNORED, depth if blind else _IGNORED, _IGNORED)
        cosmetic = _COSMETIC_THREAD_WITH_CALLOUT if request["cosmetic"] else _COSMETIC_THREAD_NONE
        values = [to_m(request["thread_depth_mm"]), _IGNORED, _IGNORED, _IGNORED, _IGNORED, _IGNORED, cosmetic,
                  _THREAD_END_BLIND, _IGNORED, _IGNORED, _IGNORED, _IGNORED]
    else:
        # Counterbore: cbore dia, cbore depth, head clearance, screw fit, drill angle, csinks, offset.
        # Countersink: near csink dia, near csink angle, head clearance, screw fit, drill angle, far csink, offset,
        # head clearance type.
        sizes = (_IGNORED, depth if blind else _IGNORED, _IGNORED)
        values = [_IGNORED, _IGNORED, 0.0, HOLE_FITS[request["fit"]]] + [_IGNORED] * 8
    return [
        HOLE_KINDS[kind][0], HOLE_STANDARDS[request["standard"]][0], fastener_index, request["size"],
        HOLE_END_CONDITIONS[request["end_condition"]], *(float(v) for v in sizes), *(float(v) for v in values),
        "", False, True, True, True, True, False,
    ]


def _solid_state(doc: Any) -> tuple[float | None, int]:
    """Total volume of the solid bodies in mm3 (None when one cannot be measured) and how many there are."""
    bodies = [body for body, _, _ in iter_body_context(doc)]
    volumes = [body_volume_mm3(body) for body in bodies]
    if not bodies or any(v is None for v in volumes):
        return None, len(bodies)
    return round(sum(v for v in volumes if v is not None), 4), len(bodies)


def _hole_face(doc: Any, request: dict[str, Any]) -> tuple[Any, list[float], list[float]]:
    """The face to drill, its unit plane normal and a point of its plane (metres); refuses a face that is not
    planar or cannot be found."""
    if "face_index" in request:
        faces = iter_face_objects(doc)
        index = request["face_index"]
        if index >= len(faces):
            raise ToolFailure(f"face_index {index} is out of range (0..{len(faces) - 1}).")
        face = faces[index][0]
        label = f"face {index}"
    else:
        point = request["face_point_mm"]
        clear_selection(doc)
        selected = select_at_point(doc, "FACE", [to_m(c) for c in point], 0, False)
        found = selected_objects(doc) if selected else []
        clear_selection(doc)
        if len(found) != 1:
            raise ToolFailure(f"No face could be selected at {point} mm.")
        face = found[0]
        label = f"the face at {point} mm"
    surface = safe(face, "GetSurface")
    if surface is None or safe(surface, "Identity") != 4001:
        raise ToolFailure(f"{label} is not planar; the Hole Wizard needs a planar face.")
    params = [float(v) for v in safe(surface, "PlaneParams") or []]
    if len(params) < 6 or not math.isclose(math.sqrt(sum(c * c for c in params[:3])), 1.0, abs_tol=1e-6):
        raise ToolFailure(f"The plane of {label} cannot be read.")
    return face, params[:3], params[3:6]


def _place_points(doc: Any, face: Any, points_mm: list[list[float]]) -> tuple[str, list[Any]]:
    """A sketch on the face with one point per hole centre, closed; its name and its sketch points.

    The Hole Wizard copies preselected points into its own position sketches, but the hole stays tied to this sketch:
    deleting it leaves the hole in the tree cutting nothing, without a rebuild error (measured on 2016 SP3). So the
    sketch stays, and the answer names it.
    """
    clear_selection(doc)
    if not select_object(doc, face, 0, False):
        raise ToolFailure("SOLIDWORKS refused to select the face.")
    manager = sketch_manager(doc)
    manager.InsertSketch(True)
    sketch = doc.SketchManager.ActiveSketch
    if sketch is None:
        raise ToolFailure("SOLIDWORKS did not open a sketch on the face.")
    try:
        to_sketch = [float(v) for v in value(value(sketch, "ModelToSketchTransform"), "ArrayData")]
        created = 0
        previous = bool(manager.AddToDB)
        manager.AddToDB = True
        try:
            for point in points_mm:
                local = apply_transform([to_m(c) for c in point], to_sketch)
                if manager.CreatePoint(local[0], local[1], 0.0) is not None:
                    created += 1
        finally:
            manager.AddToDB = previous
        name = open_sketch_name(doc, sketch)
    finally:
        exit_active_sketch(doc)
    feature = find_feature(doc, name)
    sketch_points = as_list(safe(safe(feature, "GetSpecificFeature2"), "GetSketchPoints2")) if feature else []
    if created != len(points_mm) or len(sketch_points) != len(points_mm):
        raise ToolFailure(f"The position sketch holds {len(sketch_points)} points instead of {len(points_mm)}.",
                          position_sketch=name)
    return name, sketch_points


def _select_points(doc: Any, sketch_points: list[Any]) -> None:
    """Preselect every hole centre and nothing else; the Hole Wizard puts one hole on each."""
    clear_selection(doc)
    accepted = sum(bool(flag_methods(p, "Select4").Select4(True, nothing())) for p in sketch_points)
    selected = len(selected_objects(doc))
    if accepted != len(sketch_points) or selected != len(sketch_points):
        raise ToolFailure(f"Only {selected} of {len(sketch_points)} hole centres could be selected.")


def _hole_readback(feature: Any, kind: str) -> tuple[dict[str, Any], dict[str, Any], list[list[float]]]:
    """IWizardHoleFeatureData2 of the feature: the identity fields, the kind's fields (mm, deg) and the hole centres
    in model millimetres. A field SOLIDWORKS refuses makes the feature unreadable."""
    definition = safe(feature, "GetDefinition")
    if definition is None:
        raise ToolFailure("SOLIDWORKS returned no definition (IWizardHoleFeatureData2) for the new hole.")
    identity: dict[str, Any] = {}
    for field in ("Type", "Standard", "Standard2", "FastenerType", "FastenerType2", "FastenerSize", "EndCondition"):
        identity[field] = read_member(definition, field)
    data: dict[str, Any] = {}
    for field in _HOLE_FIELDS[kind]:
        raw = read_member(definition, field)
        if raw is None:
            data[field] = None
        elif field in _HOLE_INTEGER_FIELDS:
            data[field] = int(raw)
        elif field in _HOLE_ANGLE_FIELDS:
            data[f"{field}_deg"] = round(to_deg(float(raw)), 6)
        else:
            data[f"{field}_mm"] = round(to_mm(float(raw)), 6)
    unreadable = [k for k, v in {**identity, **data}.items() if v is None]
    if unreadable:
        raise ToolFailure(f"The new hole's data cannot be read: {', '.join(unreadable)}.")
    try:
        centres = [[round(to_mm(float(value(p, axis))), 6) for axis in ("X", "Y", "Z")]
                   for p in as_list(value(definition, "GetSketchPoints"))]
    except Exception as exc:
        raise ToolFailure(f"The new hole's sketch points cannot be read ({exc}).") from exc
    return identity, data, centres


def _readback_problems(request: dict[str, Any], fastener_index: int, identity: dict[str, Any],
                       data: dict[str, Any], centres: list[list[float]]) -> list[str]:
    """Where the hole SOLIDWORKS built differs from the request, judged by its own data."""
    problems = []
    if identity["FastenerType2"] != fastener_index:
        problems.append(f"fastener type {identity['FastenerType2']} instead of {fastener_index}")
    if str(identity["FastenerSize"]) != request["size"]:
        problems.append(f"size {identity['FastenerSize']!r} instead of {request['size']!r}")
    if identity["EndCondition"] != HOLE_END_CONDITIONS[request["end_condition"]]:
        problems.append(f"end condition {identity['EndCondition']} instead of {request['end_condition']}")
    if request["depth_mm"] is not None:
        field = _BLIND_DEPTH_FIELDS[request["kind"]]
        if abs(data[field] - request["depth_mm"]) > _POINT_TOLERANCE_MM:
            problems.append(f"drilled depth {data[field]} mm instead of {request['depth_mm']}")
    if "fit" in request and data.get("HoleFit") != HOLE_FITS[request["fit"]]:
        problems.append(f"fit {data.get('HoleFit')} instead of {request['fit']}")
    if request["kind"] == "tapped":
        if abs(data["ThreadDepth_mm"] - request["thread_depth_mm"]) > _POINT_TOLERANCE_MM:
            problems.append(f"thread depth {data['ThreadDepth_mm']} mm instead of {request['thread_depth_mm']}")
        cosmetic = _COSMETIC_THREAD_WITH_CALLOUT if request["cosmetic"] else _COSMETIC_THREAD_NONE
        if data["CosmeticThreadType"] != cosmetic:
            problems.append(f"cosmetic thread type {data['CosmeticThreadType']} instead of {cosmetic}")
    unmatched = [list(c) for c in centres]
    for point in request["points_mm"]:
        match = next((c for c in unmatched if all(abs(a - b) <= _POINT_TOLERANCE_MM for a, b in zip(c, point))), None)
        if match is None:
            problems.append(f"no hole at {point} mm")
        else:
            unmatched.remove(match)
    if unmatched:
        problems.append(f"holes nobody asked for at {unmatched} mm")
    return problems


@tool(
    "hole_wizard",
    "Drill standard holes with the Hole Wizard (IFeatureManager.HoleWizard5): clearance, tapped (cosmetic thread), "
    "counterbore or countersink, sized from the Toolbox standard, fastener table and size, on one planar face at one "
    "or more points in one feature. The fastener is the Toolbox table name as the Hole Wizard lists it, e.g. 'Screw "
    "Clearances', 'Tapped hole', 'Hex Socket Head ISO 4762', 'Hex Socket CTSK Head ISO 10642'; size e.g. 'M6'. "
    "Points are model coordinates in millimetres on the face's plane. The tool draws the hole centres in a position "
    "sketch on the face that stays in the tree in front of the hole: deleting it silently removes the hole's cut. "
    "ok is true only after SOLIDWORKS' own hole data (IWizardHoleFeatureData2) reads back the requested fastener, size, "
    "end condition, blind depth, fit or thread and one hole per point, no new rebuild error, the solid volume dropped "
    "and the body count stayed. On any failure the new features are deleted again and the answer says whether the "
    "tree is as before.",
    {
        "kind": {"type": "string", "enum": sorted(HOLE_KINDS)},
        "standard": {"type": "string", "enum": sorted(HOLE_STANDARDS),
                     "description": "Hole standard; checked live on 2016 SP3 with iso only."},
        "fastener": {"type": "string", "description": "Toolbox fastener table name (Title), e.g. 'Screw Clearances'."},
        "size": {"type": "string", "description": "Size as the table lists it, e.g. 'M6' (not 'M6x1.0')."},
        "fit": {"type": "string", "enum": sorted(HOLE_FITS), "default": "normal",
                "description": "Screw fit for clearance, counterbore and countersink holes."},
        "end_condition": {"type": "string", "enum": sorted(HOLE_END_CONDITIONS)},
        "depth_mm": {"type": "number", "exclusiveMinimum": 0,
                     "description": "Drilled depth for blind holes (to the drill shoulder; the 118-degree tip adds)."},
        "thread_depth_mm": {"type": "number", "exclusiveMinimum": 0, "description": "Thread depth, tapped holes only."},
        "cosmetic": {"type": "boolean", "default": True,
                     "description": "Tapped holes: cosmetic thread with callout (true) or none (false). Threads are "
                     "never cut physically."},
        "face_index": {"type": "integer", "description": "Planar face from list_faces."},
        "face_point_mm": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                          "description": "A point on the face, instead of face_index."},
        "points": {"type": "array", "minItems": 1, "items": _POINT_SCHEMA,
                   "description": "Hole centres in model coordinates on the face's plane."},
        "name": {"type": "string", "description": "Rename the hole feature."},
    },
    ["kind", "standard", "fastener", "size", "end_condition", "points"],
)
def hole_wizard(args: dict[str, Any]) -> dict[str, Any]:
    request = _hole_wizard_args(args)
    app, doc = require_part()
    fastener_index = _toolbox_lookup(_toolbox_database(app), request["standard"], request["fastener"],
                                     request["size"], request["kind"])
    exit_active_sketch(doc)
    face, normal, root = _hole_face(doc, request)
    off_plane = [p for p in request["points_mm"]
                 if abs(sum(n * (to_m(c) - r) for n, c, r in zip(normal, p, root))) > to_m(_POINT_TOLERANCE_MM)]
    if off_plane:
        return result(False, f"Points off the face's plane: {off_plane}. Nothing was created.")
    volume_before, bodies_before = _solid_state(doc)
    if volume_before is None:
        return result(False, "The solid volume cannot be measured before drilling. Nothing was created.")
    before = feature_names(doc)
    problems_before = whats_wrong(doc)
    reported = {"kind": request["kind"], "standard": request["standard"], "fastener": request["fastener"],
                "fastener_index": fastener_index, "size": request["size"], "end_condition": request["end_condition"],
                "volume_before_mm3": volume_before}

    def failed(why: str, **data: Any) -> dict[str, Any]:
        """Delete everything this call added; the answer says whether the tree is back to what it was."""
        added = features_added(doc, before)
        names = [str(feature_property(f, "Name", "")) for f in added]
        clean = delete_features(doc, added) if added else True
        rebuild(doc)
        left = [n for n in feature_names(doc) if n not in set(before)]
        tail = (" The new features were deleted again." if added and clean and not left else
                " Nothing was left behind." if not added else f" Left behind: {left}.")
        return result(False, why + tail, **reported, **data, deleted=[n for n in names if n not in left],
                      left_behind=left)

    try:
        sketch_name, sketch_points = _place_points(doc, face, request["points_mm"])
        _select_points(doc, sketch_points)
        call = _hole_wizard_call(request, fastener_index)
        feature = feature_manager(doc).HoleWizard5(*call)
        clear_selection(doc)
        if feature is None:
            return failed("HoleWizard5 returned no feature.")
        name = str(feature_property(feature, "Name", ""))
        if name not in {str(feature_property(f, "Name", "")) for f in features_added(doc, before)}:
            return failed(f"HoleWizard5 returned '{name}', which is not a new feature in the tree.")
        rebuild(doc)
        problems = [p for p in whats_wrong(doc) if p not in problems_before]
        if problems:
            return failed(f"'{name}' has rebuild errors.", problems=problems)
        identity, data, centres = _hole_readback(feature, request["kind"])
        mismatches = _readback_problems(request, fastener_index, identity, data, centres)
        if mismatches:
            return failed(f"'{name}' is not the requested hole: {'; '.join(mismatches)}.", hole_data=data,
                          points_mm=centres)
        volume_after, bodies_after = _solid_state(doc)
        if volume_after is None:
            return failed(f"The solid volume cannot be measured after '{name}'.")
        if volume_before - volume_after < _REMOVED_MIN_MM3:
            return failed(f"'{name}' removed no material ({volume_before} -> {volume_after} mm3).",
                          volume_after_mm3=volume_after)
        if bodies_after != bodies_before:
            return failed(f"'{name}' changed the body count from {bodies_before} to {bodies_after}.",
                          volume_after_mm3=volume_after)
    except ToolFailure as exc:
        return failed(str(exc), **exc.data)
    except Exception as exc:
        logger.exception("hole_wizard failed")
        return failed(f"SOLIDWORKS COM error: {exc}")

    renamed = rename_feature(feature, request.get("name"))
    if renamed:
        name = renamed
    subfeatures = []
    sub = safe(feature, "GetFirstSubFeature")
    while sub is not None and len(subfeatures) < 20:
        subfeatures.append({"name": str(feature_property(sub, "Name", "")),
                            "type": str(feature_property(sub, "GetTypeName2", ""))})
        sub = safe(sub, "GetNextSubFeature")
    hole_type = identity["Type"]
    return result(
        True,
        f"Created '{name}' with {len(centres)} hole(s); position sketch '{sketch_name}' stays in front of it.",
        feature=name, position_sketch=sketch_name, **reported,
        type={"value": hole_type, "name": _HOLE_TYPE_NAMES.get(int(hole_type), "unknown")},
        fastener_readback=identity["FastenerType"], hole_data=data, points_mm=centres,
        volume_after_mm3=volume_after, removed_mm3=round(volume_before - volume_after, 4), bodies=bodies_after,
        subfeatures=subfeatures, **({"fit": request["fit"]} if "fit" in request else {}),
        **({"depth_mm": request["depth_mm"]} if request["depth_mm"] is not None else {}),
        **({"thread_depth_mm": request["thread_depth_mm"], "cosmetic": request["cosmetic"]}
           if request["kind"] == "tapped" else {}),
    )


# --------------------------------------------------------------------------
# Swept and lofted features
# --------------------------------------------------------------------------


@tool(
    "sweep",
    "Sweep a closed profile sketch along a path sketch.",
    {
        "profile_sketch": {"type": "string", "description": "Sketch feature name of the closed profile."},
        "path_sketch": {"type": "string", "description": "Sketch feature name of the path."},
        "cut": {"type": "boolean", "default": False},
        "merge": {"type": "boolean", "default": True},
        "keep_tangency": {"type": "boolean", "default": False},
        "twist_angle_deg": {"type": "number", "default": 0},
        "name": {"type": "string"},
    },
    ["profile_sketch", "path_sketch"],
)
def sweep(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    # The sweep APIs read the profile from mark 1 and the path from mark 4.
    require_selection(doc, {"sketches": [str(args["profile_sketch"])]}, mark=1)
    require_selection(doc, {"sketches": [str(args["path_sketch"])]}, mark=4, append=True)

    manager = feature_manager(doc)
    keep_tangency = bool(args.get("keep_tangency", False))
    twist = to_rad(args.get("twist_angle_deg", 0))
    if bool(args.get("cut", False)):
        # InsertCutSwept5 has no Merge flag but does carry assembly scope flags.
        # InsertCutSwept4 (2016) is the same list without the last three.
        cut_args = (
            False, True, 0, keep_tangency, False,
            0, 0, False, 0.0, 0.0, 0, 0,
            True, True, twist, False,
            False, False, False,
            False, 0.0, 0,
        )
        feature = call_versioned(
            manager,
            ("InsertCutSwept5", cut_args),
            ("InsertCutSwept4", cut_args[:19]),
        )
    else:
        # InsertProtrusionSwept3 (2016) stops after BMergeSmoothFaces.
        boss_args = (
            False, True, 0, keep_tangency, False,
            0, 0, False, 0.0, 0.0, 0, 0,
            bool(args.get("merge", True)), True, True,
            twist, False, False, 0.0, 0,
        )
        feature = call_versioned(
            manager,
            ("InsertProtrusionSwept4", boss_args),
            ("InsertProtrusionSwept3", boss_args[:17]),
        )
    rename_feature(feature, args.get("name"))
    return feature_result(
        doc, feature, "sweep",
        profile=str(args["profile_sketch"]), path=str(args["path_sketch"]),
    )


@tool(
    "loft",
    "Loft a solid through two or more profile sketches, in the order given.",
    {
        "profile_sketches": {
            "type": "array",
            "minItems": 2,
            "items": {"type": "string"},
            "description": "Sketch feature names, ordered from one end of the loft to the other.",
        },
        "cut": {"type": "boolean", "default": False},
        "closed": {"type": "boolean", "default": False},
        "keep_tangency": {"type": "boolean", "default": False},
        "merge": {"type": "boolean", "default": True},
        "name": {"type": "string"},
    },
    ["profile_sketches"],
)
def loft(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    profiles = [str(name) for name in args["profile_sketches"]]
    clear_selection(doc)
    for index, name in enumerate(profiles):
        require_selection(doc, {"sketches": [name]}, mark=1, append=index > 0)

    manager = feature_manager(doc)
    closed = bool(args.get("closed", False))
    keep_tangency = bool(args.get("keep_tangency", False))
    if bool(args.get("cut", False)):
        # InsertCutBlend takes neither tangent-length nor merge arguments.
        feature = manager.InsertCutBlend(
            closed, keep_tangency, False, 1.0,
            0, 0, False, 0.0, 0.0, 0, True, True,
        )
    else:
        feature = manager.InsertProtrusionBlend2(
            closed, keep_tangency, False, 1.0,
            0, 0, 0.0, 0.0, True, True,
            False, 0.0, 0.0, 0,
            bool(args.get("merge", True)), True, True, 0,
        )
    rename_feature(feature, args.get("name"))
    return feature_result(doc, feature, "loft", profiles=profiles)


# --------------------------------------------------------------------------
# Patterns and mirror
# --------------------------------------------------------------------------


@tool(
    "linear_pattern",
    "Repeat features along one or two directions. Put the features to repeat in selection and the "
    "direction reference (an edge, axis, or plane) in direction1_selection. Spacing is millimetres.",
    {
        "selection": SELECTION_SCHEMA,
        "direction1_selection": SELECTION_SCHEMA,
        "count1": {"type": "integer", "minimum": 2, "default": 2},
        "spacing1_mm": {"type": "number", "exclusiveMinimum": 0},
        "reverse1": {"type": "boolean", "default": False},
        "direction2_selection": SELECTION_SCHEMA,
        "count2": {"type": "integer", "minimum": 1, "default": 1},
        "spacing2_mm": {"type": "number", "default": 0},
        "reverse2": {"type": "boolean", "default": False},
        "geometry_pattern": {"type": "boolean", "default": False},
        "name": {"type": "string"},
    },
    ["selection", "direction1_selection", "spacing1_mm"],
)
def linear_pattern(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    # Marks: 1 = direction 1, 2 = direction 2, 4 = the features being patterned.
    require_selection(doc, args["direction1_selection"], mark=1)
    has_second = bool(args.get("direction2_selection")) and int(args.get("count2", 1)) > 1
    if has_second:
        require_selection(doc, args["direction2_selection"], mark=2, append=True)
    require_selection(doc, args["selection"], mark=4, append=True)

    pattern_args = (
        int(args.get("count1", 2)), to_m(args["spacing1_mm"]),
        int(args.get("count2", 1)) if has_second else 1,
        to_m(args.get("spacing2_mm", 0)) if has_second else 0.0,
        bool(args.get("reverse1", False)), bool(args.get("reverse2", False)),
        "", "",
        bool(args.get("geometry_pattern", False)), False,
        False, False, True, True, False, False, False, False, 0.0, 0.0,
        False, False,
    )
    # FeatureLinearPattern4 (2016) ends at Offset2, i.e. the first 20 arguments.
    feature = call_versioned(
        feature_manager(doc),
        ("FeatureLinearPattern5", pattern_args),
        ("FeatureLinearPattern4", pattern_args[:20]),
    )
    rename_feature(feature, args.get("name"))
    return feature_result(doc, feature, "linear pattern", count1=int(args.get("count1", 2)))


@tool(
    "circular_pattern",
    "Repeat features around an axis. Put the features in selection and the axis (a reference axis, "
    "a cylindrical face, or a straight edge) in axis_selection. Angle is degrees.",
    {
        "selection": SELECTION_SCHEMA,
        "axis_selection": SELECTION_SCHEMA,
        "count": {"type": "integer", "minimum": 2, "default": 4},
        "angle_deg": {"type": "number", "default": 360, "description": "Total spread when equal_spacing is true, otherwise the step angle."},
        "equal_spacing": {"type": "boolean", "default": True},
        "reverse": {"type": "boolean", "default": False},
        "geometry_pattern": {"type": "boolean", "default": False},
        "name": {"type": "string"},
    },
    ["selection", "axis_selection"],
)
def circular_pattern(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    # Marks: 1 = the pattern axis, 4 = the features being patterned.
    require_selection(doc, args["axis_selection"], mark=1)
    require_selection(doc, args["selection"], mark=4, append=True)

    # SOLIDWORKS always stores a per-instance step; for equal spacing it wants
    # the full spread, which it then divides internally.
    spacing = to_rad(args.get("angle_deg", 360))
    pattern_args = (
        int(args.get("count", 4)), spacing,
        bool(args.get("reverse", False)), "",
        bool(args.get("geometry_pattern", False)),
        bool(args.get("equal_spacing", True)), False, False,
        False, False, 1, 0.0, "", False,
    )
    # FeatureCircularPattern4 (2016) ends at VaryInstance, the first 7 arguments.
    feature = call_versioned(
        feature_manager(doc),
        ("FeatureCircularPattern5", pattern_args),
        ("FeatureCircularPattern4", pattern_args[:7]),
    )
    rename_feature(feature, args.get("name"))
    return feature_result(doc, feature, "circular pattern", count=int(args.get("count", 4)))


@tool(
    "mirror_feature",
    "Mirror features (or whole bodies) about a plane or planar face. Put the features in selection "
    "and the mirror plane in plane_selection.",
    {
        "selection": SELECTION_SCHEMA,
        "plane_selection": SELECTION_SCHEMA,
        "mirror_body": {"type": "boolean", "default": False, "description": "Mirror the whole body instead of features."},
        "merge": {"type": "boolean", "default": True},
        "geometry_pattern": {"type": "boolean", "default": False},
        "name": {"type": "string"},
    },
    ["selection", "plane_selection"],
)
def mirror_feature(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    exit_active_sketch(doc)
    # Marks: 2 = the mirror plane, 1 = the features being mirrored.
    require_selection(doc, args["plane_selection"], mark=2)
    require_selection(doc, args["selection"], mark=1, append=True)

    feature = feature_manager(doc).InsertMirrorFeature2(
        bool(args.get("mirror_body", False)),
        bool(args.get("geometry_pattern", False)),
        bool(args.get("merge", True)),
        False, 0,
    )
    rename_feature(feature, args.get("name"))
    return feature_result(doc, feature, "mirror")


# --------------------------------------------------------------------------
# Feature-tree maintenance
# --------------------------------------------------------------------------


@tool(
    "delete_feature",
    "Delete named features from the feature tree, together with anything that depends on them.",
    {"feature_names": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
    ["feature_names"],
)
def delete_feature(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = active_document()
    exit_active_sketch(doc)
    clear_selection(doc)
    names = [str(name) for name in args["feature_names"]]
    selected: list[str] = []
    for name in names:
        feature = find_feature(doc, name)
        if feature is None:
            return result(False, f"No feature named '{name}' exists in this document.")
        if bool(selectable(feature).Select2(True, 0)):
            selected.append(name)
    if not selected:
        return result(False, "Could not select any of the named features.")
    deleted = bool(extension(doc).DeleteSelection2(0))
    rebuild(doc)
    clear_selection(doc)
    return result(
        deleted,
        f"Deleted {len(selected)} features." if deleted else "SOLIDWORKS refused to delete the selection.",
        deleted=selected,
    )


@tool(
    "rename_feature",
    "Rename a feature so later tool calls can refer to it stably.",
    {"feature_name": {"type": "string"}, "new_name": {"type": "string"}},
    ["feature_name", "new_name"],
)
def rename_feature_tool(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = active_document()
    feature = find_feature(doc, str(args["feature_name"]))
    if feature is None:
        return result(False, f"No feature named '{args['feature_name']}' exists in this document.")
    feature.Name = str(args["new_name"])
    return result(True, f"Renamed to '{args['new_name']}'.", feature=str(args["new_name"]))


@tool(
    "set_feature_suppression",
    "Suppress or unsuppress named features.",
    {
        "feature_names": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "suppressed": {"type": "boolean", "default": True},
    },
    ["feature_names"],
)
def set_feature_suppression(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = active_document()
    exit_active_sketch(doc)
    suppressed = bool(args.get("suppressed", True))
    changed: list[str] = []
    for name in args["feature_names"]:
        feature = find_feature(doc, str(name))
        if feature is None:
            return result(False, f"No feature named '{name}' exists in this document.")
        clear_selection(doc)
        if not bool(selectable(feature).Select2(False, 0)):
            return result(False, f"Could not select feature '{name}'.")
        # These take no arguments, and pywin32 may already have evaluated them
        # on attribute access, so go through value() rather than calling.
        ok = bool(value(doc, "EditSuppress2" if suppressed else "EditUnsuppress2"))
        if ok:
            changed.append(str(name))
    rebuild(doc)
    clear_selection(doc)
    return result(
        bool(changed),
        f"{'Suppressed' if suppressed else 'Unsuppressed'} {len(changed)} features.",
        changed=changed,
        problems=whats_wrong(doc),
    )
