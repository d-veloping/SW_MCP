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

"""Sheet metal: base flange, edge flange, miter flange, hem, closed and broken
corners, the flat pattern, and its DXF/DWG export.

Measured on SOLIDWORKS 2016 SP3 (ClauSW, 2026-10-06):

* The IFeatureManager variants that take a ``CustomBendAllowance`` argument
  (InsertSheetMetalBaseFlange2, InsertSheetMetalHem2) create nothing through
  late binding whatever is passed for it; the IModelDoc2 variants without
  that argument build the feature.  InsertSheetMetalEdgeFlange2 and
  InsertSheetMetalMiterFlange on the feature manager do work.
* Several of these calls raise ``Ausnahmefehler des Servers`` (0x80010105)
  on return although the feature was built, so every creating call is judged
  by the feature tree and the geometry, never by its return value.
* An edge flange needs a profile sketch: InsertSketchForEdgeFlange creates
  the sketch on the flange plane, and the profile is drawn into it in sketch
  coordinates.  Without a sketch the call creates nothing.
* IModelDoc2::SetBendState reports success and changes nothing; the flat
  pattern is reached by unsuppressing the Flat-Pattern feature.
* IPartDoc::ExportFlatPatternView opens a file dialog, which would wedge the
  server; ExportToDWG2 with VT_NULL for the views argument exports silently
  and needs the document saved, because it takes the model path.
* A corner relief is built corner by corner: the two bend faces (the
  cylindrical faces) that meet at the corner are selected with mark 4,
  AddCornerReliefCorner and AddCornerReliefType follow, and FinishCornerRelief
  makes the feature.  Selecting the flat faces instead raises and builds
  nothing.
* The K-factor cannot be changed through ISheetMetalFeatureData or
  ICustomBendAllowance here: the setters are accepted and ignored.  The
  document default (0.48 on this install) is what the flat pattern uses.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

import pythoncom

from .sw_core import (
    bodies_extents,
    box_mm,
    clear_selection,
    delete_features,
    dispatch_array,
    feature_names,
    features_added,
    double_array,
    exit_active_sketch,
    feature_manager,
    feature_property,
    feature_result,
    flag_methods,
    get_bodies,
    iter_edge_objects,
    iter_face_objects,
    iter_feature_objects,
    logger,
    null_variant,
    readback_mismatches,
    rebuild,
    rename_feature,
    require_part,
    require_selection,
    result,
    safe,
    selectable,
    SELECTION_SCHEMA,
    select_object,
    select_sketch_for_feature,
    to_deg,
    to_m,
    to_mm,
    to_rad,
    tool,
    value,
    whats_wrong,
)


# swFlangePositionTypes_e
FLANGE_POSITIONS = {"material_inside": 1, "material_outside": 2, "bend_outside": 3}
# swFlangeDimTypes_e.  InsertSheetMetalEdgeFlange2 measures its length from
# the inner virtual sharp whatever dim type it is given: outer_virtual_sharp
# builds the same geometry and only reads back length + t*tan(angle/2), and
# bend_tangent builds nothing (measured on 2016 SP3, 2026-10-08).  The tool
# therefore always passes the inner virtual sharp.
FLANGE_LENGTH_INNER_VIRTUAL_SHARP = 2
# swSheetMetalReliefTypes_e
RELIEF_TYPES = {"rectangular": 1, "tear": 2, "obround": 3, "none": 4}
# swInsertEdgeFlangeOptions_e
EDGE_FLANGE_USE_DEFAULT_RADIUS = 1
EDGE_FLANGE_USE_RELIEF_RATIO = 64
EDGE_FLANGE_USE_DEFAULT_RELIEF = 128
# swHemTypes_e / swHemPositionTypes_e
HEM_TYPES = {"open": 0, "closed": 1, "tear_drop": 2, "rolled": 3, "double": 4}
HEM_POSITIONS = {"inside": 0, "outside": 1}
# swClosedCornerTypes_e
CLOSED_CORNER_TYPES = {"butt": 1, "overlap": 2, "underlap": 3}
# swBreakCornerTypes_e
BREAK_CORNER_TYPES = {"fillet": 0, "chamfer": 1}
# swCornerReliefType_e
CORNER_RELIEF_TYPES = {"circular": 0, "square": 1, "bend_waist": 2, "tear": 3, "constant_width": 4, "obround": 5}
# swSMBendState_e
BEND_STATES = {0: "none", 1: "sharps", 2: "flattened", 3: "folded"}
# swBendAllowanceTypes_e
BEND_ALLOWANCE_TYPES = {1: "bend_table", 2: "k_factor", 3: "bend_allowance", 4: "bend_deduction", 5: "bend_calculation_table"}

SHEET_METAL_FEATURE_TYPES = {
    "SheetMetal": "sheet_metal",
    "SMBaseFlange": "base_flange",
    "EdgeFlange": "edge_flange",
    "SMMiteredFlange": "miter_flange",
    "Hem": "hem",
    "CornerFeat": "closed_corner",
    "BreakCorner": "break_corner",
    "CornerRelief": "corner_relief",
    "FlatPattern": "flat_pattern",
    "OneBend": "bend",
    "SketchBend": "sketched_bend",
    "Jog": "jog",
}

# IPartDoc::ExportToDWG2 sheet-metal option bits
EXPORT_GEOMETRY = 1
EXPORT_HIDDEN_EDGES = 2
EXPORT_BEND_LINES = 4
EXPORT_SKETCHES = 8
EXPORT_MERGE_COPLANAR = 16
EXPORT_LIBRARY_FEATURES = 32
EXPORT_FORMING_TOOLS = 64
EXPORT_SHEET_METAL = 1  # swExportToDWG_e.swExportToDWG_ExportSheetMetal

FLAT_PATTERN_EXTENSIONS = {".dxf", ".dwg"}

# The raise that several sheet-metal members make on return, after building
# the feature: RPC_E_SERVERFAULT.
_SERVER_FAULT = -2147417851


def is_server_fault(exc: Exception) -> bool:
    if getattr(exc, "hresult", None) == _SERVER_FAULT:
        return True
    args = getattr(exc, "args", ())
    return bool(args) and args[0] == _SERVER_FAULT


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _build(doc: Any, action: str, call: Any, wanted_types: tuple[str, ...]) -> tuple[Any, list[Any]]:
    """Run a sheet-metal creating call and recover the feature from the tree.

    Returns the created feature of one of ``wanted_types`` (or None) and
    every feature the call added.  A server fault on return is swallowed when
    the tree shows the feature, and logged when it does not.  When the call
    built no such feature, or raised any other error, whatever it did add is
    deleted again before returning or re-raising, so a failed call leaves
    nothing of its own in the tree.
    """
    before = feature_names(doc)
    fault: Exception | None = None
    try:
        call()
    except Exception as exc:
        if not is_server_fault(exc):
            delete_features(doc, features_added(doc, before))
            raise
        fault = exc
    created = features_added(doc, before)
    for feature in created:
        if str(feature_property(feature, "GetTypeName2", "")) in wanted_types:
            return feature, created
    if fault is not None:
        logger.info("SOLIDWORKS raised on %s and built nothing: %s", action, fault)
    delete_features(doc, created)
    return None, created


def _volume_mm3(doc: Any) -> float | None:
    try:
        from .sw_inspect import get_mass_properties

        payload = get_mass_properties({})
        return payload["data"]["volume_mm3"] if payload.get("ok") else None
    except Exception:
        return None


def _size_mm(doc: Any) -> list[float] | None:
    """Exact overall size of the part's solid bodies, from their extreme points."""
    try:
        box = bodies_extents(get_bodies(doc))
    except Exception:
        return None
    return None if box is None else box_mm(box)["size_mm"]


def _sheet_result(doc: Any, feature: Any, action: str, **data: Any) -> dict[str, Any]:
    """feature_result plus the two numbers that tell whether sheet metal was built."""
    payload = feature_result(doc, feature, action, **data)
    if feature is not None:
        payload["data"]["volume_mm3"] = _volume_mm3(doc)
        payload["data"]["size_mm"] = _size_mm(doc)
    return payload


def _definition(feature: Any) -> Any:
    try:
        return value(feature, "GetDefinition")
    except Exception:
        return None


def _release_selection_access(definition: Any) -> None:
    try:
        definition.ReleaseSelectionAccess()
    except Exception:
        logger.info("Could not release the selection access of a feature definition")


def _modify(feature: Any, doc: Any, definition: Any) -> bool:
    """IFeature::ModifyDefinition; the caller judges it by reading the definition back."""
    try:
        return bool(feature.ModifyDefinition(definition, doc, pythoncom.Nothing))
    except Exception as exc:
        if is_server_fault(exc):
            return True
        raise


def corner_face_pairs(corners: list[dict[str, Any]], face_count: int) -> list[list[int]]:
    """The face index pairs of a corner-relief request, checked before any COM call.

    AddCornerReliefCorner starts a build inside SOLIDWORKS; an index found bad
    only at the second corner would leave that build open, so every index is
    validated first.
    """
    pairs = []
    for corner in corners:
        pair = [int(i) for i in corner["faces"]]
        if len(pair) != 2:
            raise RuntimeError(f"Each corner needs exactly two bend faces; got {pair}.")
        for index in pair:
            if not 0 <= index < face_count:
                raise RuntimeError(f"Face index {index} is out of range (0..{face_count - 1}); call list_faces first.")
        pairs.append(pair)
    return pairs


def _finish_corner_relief_quietly(manager: Any) -> None:
    try:
        manager.FinishCornerRelief()
    except Exception:
        pass


def _features_of_type(doc: Any, *type_names: str) -> list[Any]:
    return [f for f in iter_feature_objects(doc) if str(feature_property(f, "GetTypeName2", "")) in type_names]


def _sheet_metal_feature(doc: Any) -> Any | None:
    features = _features_of_type(doc, "SheetMetal")
    return features[0] if features else None


def _flat_pattern_features(doc: Any) -> list[Any]:
    """Every Flat-Pattern feature: one per sheet metal body."""
    return _features_of_type(doc, "FlatPattern")


def _single_flat_pattern(doc: Any) -> Any | None:
    """The part's one Flat-Pattern feature, or None when it has none.

    A multibody sheet metal part carries one Flat-Pattern per body, and a
    sheet body next to a plain solid body would be measured together with it;
    flattening or exporting either would have to address bodies one by one,
    which these tools do not, so they refuse any part with more than one solid
    body or more than one Flat-Pattern rather than report one body as the part.
    """
    features = _flat_pattern_features(doc)
    bodies = len(get_bodies(doc))
    if len(features) > 1 or bodies > 1:
        raise RuntimeError(
            f"The part has {bodies} solid bodies and {len(features)} Flat-Pattern features; "
            "sheet_metal_flatten and export_flat_pattern handle single-body sheet metal parts only."
        )
    return features[0] if features else None


def _require_sheet_metal(doc: Any) -> Any:
    feature = _sheet_metal_feature(doc)
    if feature is None:
        raise RuntimeError("The active part is not a sheet metal part. Create a base flange first.")
    return feature


def _sheet_metal_parameters(doc: Any) -> dict[str, Any]:
    feature = _sheet_metal_feature(doc)
    if feature is None:
        return {}
    definition = _definition(feature)
    if definition is None:
        return {"feature": str(feature_property(feature, "Name", ""))}
    allowance = safe(definition, "BendAllowanceType")
    relief_names = {v: k for k, v in RELIEF_TYPES.items()}
    return {
        "feature": str(feature_property(feature, "Name", "")),
        "thickness_mm": round(to_mm(safe(definition, "Thickness", 0.0) or 0.0), 6),
        "bend_radius_mm": round(to_mm(safe(definition, "BendRadius", 0.0) or 0.0), 6),
        "bend_allowance_type": BEND_ALLOWANCE_TYPES.get(int(allowance or 0), str(allowance)),
        "k_factor": safe(definition, "KFactor"),
        "bend_allowance_mm": round(to_mm(safe(definition, "BendAllowance", 0.0) or 0.0), 6),
        "auto_relief": bool(safe(definition, "UseAutoRelief", False)),
        "relief_type": relief_names.get(int(safe(definition, "AutoReliefType", 0) or 0)),
        "relief_ratio": safe(definition, "ReliefRatio"),
    }


def _bend_state(doc: Any) -> str:
    try:
        state = int(value(doc, "GetBendState"))
    except Exception:
        return "unknown"
    return BEND_STATES.get(state, str(state))


def _is_flattened(doc: Any) -> bool:
    """True only when every Flat-Pattern feature is unsuppressed."""
    features = _flat_pattern_features(doc)
    return bool(features) and not any(bool(safe(f, "IsSuppressed", True)) for f in features)


def _edge_objects(doc: Any, indices: list[int]) -> list[Any]:
    edges = iter_edge_objects(doc)
    chosen = []
    for raw in indices:
        index = int(raw)
        if not 0 <= index < len(edges):
            raise RuntimeError(f"Edge index {index} is out of range (0..{len(edges) - 1}); call list_edges first.")
        edge = edges[index][0]
        curve = value(edge, "GetCurve")
        if not bool(safe(curve, "IsLine", False)):
            raise RuntimeError(f"Edge {index} is not a straight edge; a flange needs straight edges.")
        chosen.append(edge)
    return chosen


def _edge_endpoints_m(edge: Any) -> tuple[list[float], list[float]]:
    start = list(value(value(edge, "GetStartVertex"), "GetPoint"))[:3]
    end = list(value(value(edge, "GetEndVertex"), "GetPoint"))[:3]
    return start, end


def _to_sketch_space(app: Any, sketch: Any, point_m: list[float]) -> list[float]:
    transform = value(sketch, "ModelToSketchTransform")
    utility = flag_methods(app.GetMathUtility, "CreatePoint")
    moved = utility.CreatePoint(double_array(point_m)).MultiplyTransform(transform)
    return list(moved.ArrayData)[:3]


def _draw_flange_profile(app: Any, doc: Any, edge: Any, angle_rad: float, flip: bool, length_m: float) -> tuple[Any, Any]:
    """Make the profile sketch an edge flange needs: a rectangle on the edge.

    InsertSketchForEdgeFlange creates an empty sketch on the plane the flange
    will lie in, with the edge along its x axis; the flange goes towards +y.
    """
    sketch_feature = doc.InsertSketchForEdgeFlange(edge, angle_rad, bool(flip))
    if sketch_feature is None:
        raise RuntimeError("SOLIDWORKS could not create the flange profile sketch for that edge.")
    try:
        return sketch_feature, _fill_flange_profile(app, doc, sketch_feature, edge, length_m)
    except Exception:
        # A sketch that could not be filled is no profile; leave none behind.
        exit_active_sketch(doc)
        delete_features(doc, [sketch_feature])
        raise


def _fill_flange_profile(app: Any, doc: Any, sketch_feature: Any, edge: Any, length_m: float) -> Any:
    sketch = value(sketch_feature, "GetSpecificFeature2")
    start_m, end_m = _edge_endpoints_m(edge)
    start = _to_sketch_space(app, sketch, start_m)
    end = _to_sketch_space(app, sketch, end_m)
    clear_selection(doc)
    if not bool(selectable(sketch_feature).Select2(False, 0)):
        raise RuntimeError("Could not select the flange profile sketch.")
    doc.EditSketch()
    doc.SetAddToDB(True)
    doc.SetDisplayWhenAdded(False)
    try:
        corners = [
            (start[0], start[1]), (end[0], end[1]),
            (end[0], end[1] + length_m), (start[0], start[1] + length_m),
        ]
        for index, (x, y) in enumerate(corners):
            nx, ny = corners[(index + 1) % 4]
            doc.CreateLine2(x, y, 0.0, nx, ny, 0.0)
    finally:
        doc.SetDisplayWhenAdded(True)
        doc.SetAddToDB(False)
        doc.InsertSketch2(True)
    return sketch


def _dxf_entities(text: list[str]) -> list[dict[str, list[str]]]:
    """The ENTITIES section as one dict per entity, every group code a list.

    A polyline repeats group 10/20 for each vertex, so values are never
    collapsed to the first occurrence.
    """
    entities: list[dict[str, list[str]]] = []
    section = None
    current: dict[str, list[str]] | None = None
    for index in range(0, len(text) - 1, 2):
        code, raw = text[index].strip(), text[index + 1].strip()
        if code == "2" and index >= 2 and text[index - 1].strip() == "SECTION":
            section = raw
            continue
        if section != "ENTITIES":
            continue
        if code == "0":
            if current is not None:
                entities.append(current)
            if raw == "ENDSEC":
                current, section = None, None
            else:
                current = {"type": [raw]}
        elif current is not None:
            current.setdefault(code, []).append(raw)
    if current is not None:
        entities.append(current)
    return entities


def _floats(entity: dict[str, list[str]], code: str) -> list[float]:
    values = []
    for raw in entity.get(code, []):
        try:
            values.append(float(raw))
        except ValueError:
            pass
    return values


def dxf_entity_points(entity: dict[str, list[str]]) -> list[tuple[float, float]]:
    """The extreme points of one entity's geometry, for the drawing extents.

    LINE: both ends.  CIRCLE: the four axis points.  ARC: both ends plus
    every axis crossing inside the swept angle (DXF arcs run counterclockwise
    from group 50 to group 51).  Polylines: every vertex, which bounds the
    geometry only while no vertex carries a bulge (see ``dxf_curved_unparsed``).
    Other entities contribute nothing rather than a misleading centre point.
    """
    kind = entity["type"][0]
    xs, ys = _floats(entity, "10"), _floats(entity, "20")
    if kind == "LINE":
        return list(zip(xs + _floats(entity, "11"), ys + _floats(entity, "21")))
    if kind in ("LWPOLYLINE", "POLYLINE", "VERTEX"):
        return list(zip(xs, ys))
    if kind in ("CIRCLE", "ARC") and xs and ys:
        cx, cy = xs[0], ys[0]
        radii = _floats(entity, "40")
        if not radii:
            return []
        r = radii[0]
        if kind == "CIRCLE":
            return [(cx + r, cy), (cx - r, cy), (cx, cy + r), (cx, cy - r)]
        start = (_floats(entity, "50") or [0.0])[0] % 360.0
        end = (_floats(entity, "51") or [360.0])[0] % 360.0
        sweep = (end - start) % 360.0 or 360.0
        points = [
            (cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
            for a in (start, start + sweep)
        ]
        for axis in (0.0, 90.0, 180.0, 270.0):
            if (axis - start) % 360.0 <= sweep:
                points.append((cx + r * math.cos(math.radians(axis)), cy + r * math.sin(math.radians(axis))))
        return points
    return []


def dxf_curved_unparsed(entity: dict[str, list[str]]) -> bool:
    """True for geometry whose 10/20 points do not bound it.

    A spline's points are control points, an ellipse's are its centre and
    major axis, and a polyline vertex with a
    bulge (group 42) starts an arc that can swell past both of its ends.
    """
    kind = entity["type"][0]
    if kind in ("SPLINE", "ELLIPSE"):
        return True
    return kind in ("LWPOLYLINE", "POLYLINE", "VERTEX") and any(b != 0.0 for b in _floats(entity, "42"))


def dxf_header_value(text: list[str], variable: str) -> str | None:
    """The first value of a HEADER variable such as $INSUNITS, or None."""
    for index in range(0, len(text) - 3, 2):
        if text[index].strip() == "9" and text[index + 1].strip() == variable:
            return text[index + 3].strip()
    return None


# $INSUNITS: 4 is millimetres; SOLIDWORKS writes it (measured 2026-10-08).
DXF_UNITS_MM = "4"


def summarize_dxf(path: Path) -> dict[str, Any]:
    """Count the entities of a DXF file and tell the bend lines from the outline.

    SOLIDWORKS writes everything on layer 0 and marks bend lines only by
    their line type (CENTER*), so that is what is counted.  Extents come from
    the geometry of lines, arcs, circles and polylines, and are reported as
    extents_mm only when the header's $INSUNITS says millimetres; any other
    unit, a spline, an ellipse or a bulged polyline leaves the extents out
    with a note instead of reporting a box that may be wrong.
    """
    text = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    units = dxf_header_value(text, "$INSUNITS")
    entities = _dxf_entities(text)
    kinds = Counter(e["type"][0] for e in entities)
    bend_lines = sum(
        1 for e in entities
        if e["type"][0] == "LINE" and (e.get("6") or [""])[0].upper().startswith("CENTER")
    )
    points = [point for e in entities for point in dxf_entity_points(e)]
    outline = sum(kinds.get(k, 0) for k in ("LINE", "ARC", "CIRCLE", "ELLIPSE", "LWPOLYLINE", "POLYLINE", "SPLINE")) - bend_lines
    summary: dict[str, Any] = {"entities": dict(kinds), "bend_lines": bend_lines, "outline_entities": outline, "insunits": units}
    if units != DXF_UNITS_MM:
        summary["extents_note"] = f"Extents left out: $INSUNITS is {units!r}, not millimetres ({DXF_UNITS_MM})."
    elif any(dxf_curved_unparsed(e) for e in entities):
        summary["extents_note"] = "Extents left out: the outline has splines, ellipses or bulged polylines."
    elif points:
        xs = [x for x, _ in points]
        ys = [y for _, y in points]
        summary["extents_mm"] = [round(max(xs) - min(xs), 4), round(max(ys) - min(ys), 4)]
    return summary


def export_options(args: dict[str, Any]) -> int:
    """The SheetMetalOptions bitmask for ExportToDWG2 from the tool's flags."""
    options = EXPORT_GEOMETRY
    for key, bit in (
        ("bend_lines", EXPORT_BEND_LINES), ("sketches", EXPORT_SKETCHES), ("hidden_edges", EXPORT_HIDDEN_EDGES),
        ("library_features", EXPORT_LIBRARY_FEATURES), ("forming_tools", EXPORT_FORMING_TOOLS),
        ("merge_coplanar_faces", EXPORT_MERGE_COPLANAR),
    ):
        if bool(args.get(key, key == "bend_lines")):
            options |= bit
    return options


# --------------------------------------------------------------------------
# Creating features
# --------------------------------------------------------------------------

_SKETCH_ARG = {
    "type": "string",
    "description": "Which sketch to use as the profile. Defaults to the most recently created sketch.",
}


@tool(
    "sheet_metal_base_flange",
    "Turn a sketch into the first sheet metal body of the part. An open profile (a chain of lines, "
    "each corner becomes a bend) is thickened and extruded depth_mm; a closed profile becomes a flat "
    "plate of that outline. Thickness and bend radius are millimetres and become the part's sheet metal "
    "defaults for every later flange. Adds the Sheet-Metal and Flat-Pattern features.",
    {
        "thickness_mm": {"type": "number", "exclusiveMinimum": 0},
        "bend_radius_mm": {"type": "number", "exclusiveMinimum": 0, "description": "Inner bend radius. Defaults to the thickness."},
        "depth_mm": {"type": "number", "exclusiveMinimum": 0, "default": 10, "description": "Extrusion width of an open profile; ignored for a closed one."},
        "sketch_name": _SKETCH_ARG,
        "reverse_thickness": {"type": "boolean", "default": False, "description": "Put the material on the other side of the sketch lines."},
        "reverse_direction": {"type": "boolean", "default": False, "description": "Extrude an open profile the other way."},
        "name": {"type": "string"},
    },
    ["thickness_mm"],
)
def sheet_metal_base_flange(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    if _sheet_metal_feature(doc) is not None:
        return result(False, "This part already has a sheet metal body; use sheet_metal_edge_flange or sheet_metal_miter_flange to add to it.")
    thickness = to_m(args["thickness_mm"])
    radius = to_m(args.get("bend_radius_mm") or args["thickness_mm"])
    depth = to_m(args.get("depth_mm", 10))
    sketch = select_sketch_for_feature(doc, args.get("sketch_name"))

    feature, _ = _build(
        doc, "base flange",
        lambda: doc.InsertSheetMetalBaseFlange(
            thickness, bool(args.get("reverse_thickness", False)), radius,
            depth, 0.0, bool(args.get("reverse_direction", False)), 0, 0, 1,
        ),
        ("SMBaseFlange",),
    )
    rename_feature(feature, args.get("name"))
    payload = _sheet_result(doc, feature, "base flange", sketch=sketch, thickness_mm=args["thickness_mm"])
    if feature is None:
        payload["message"] += " A base flange wants a single open chain of lines or one closed outline, not both and no crossing segments."
        return payload
    parameters = _sheet_metal_parameters(doc)
    payload["data"]["sheet_metal"] = parameters
    wanted = {"thickness_mm": float(args["thickness_mm"]), "bend_radius_mm": args.get("bend_radius_mm")}
    mismatches = readback_mismatches(wanted, parameters, 1e-6)
    if mismatches:
        payload["ok"] = False
        payload["message"] += f" The sheet metal feature did not take {'; '.join(mismatches)}."
    return payload


@tool(
    "sheet_metal_edge_flange",
    "Add a flange along one or more straight edges of the sheet metal body, bent up from the face the "
    "edge belongs to. length_mm is the flange height measured from the inner virtual sharp of the bend; angle_deg is the "
    "bend angle (90 = perpendicular). Put the edges in selection.edges from list_edges; use an edge of "
    "the face the flange should rise from, and flip when it goes the wrong way. Several edges give one "
    "feature with one flange each.",
    {
        "selection": SELECTION_SCHEMA,
        "length_mm": {"type": "number", "exclusiveMinimum": 0},
        "angle_deg": {"type": "number", "default": 90, "exclusiveMinimum": 0, "maximum": 180},
        "bend_radius_mm": {"type": "number", "exclusiveMinimum": 0, "description": "Inner bend radius. Defaults to the part's sheet metal radius."},
        "position": {
            "type": "string", "enum": sorted(FLANGE_POSITIONS), "default": "bend_outside",
            "description": "Where the bend sits relative to the edge: material_inside keeps the outer face flush with the edge, "
                           "material_outside keeps the inner face flush, bend_outside puts the whole bend beyond the edge.",
        },
        "flip": {"type": "boolean", "default": False, "description": "Bend towards the other side of the sheet (not read back; check the box)."},
        "relief_type": {"type": "string", "enum": sorted(RELIEF_TYPES), "description": "Bend relief. Defaults to the part's automatic relief."},
        "relief_ratio": {"type": "number", "default": 0.5, "description": "Relief width as a ratio of the thickness, when relief_type is given."},
        "name": {"type": "string"},
    },
    ["selection", "length_mm"],
)
def sheet_metal_edge_flange(args: dict[str, Any]) -> dict[str, Any]:
    app, doc = require_part()
    _require_sheet_metal(doc)
    exit_active_sketch(doc)
    indices = list((args.get("selection") or {}).get("edges") or [])
    if not indices:
        return result(False, "sheet_metal_edge_flange needs selection.edges with at least one edge index from list_edges.")
    edges = _edge_objects(doc, indices)
    angle = to_rad(args.get("angle_deg", 90))
    length = to_m(args["length_mm"])
    position = FLANGE_POSITIONS[str(args.get("position", "bend_outside"))]
    reference = FLANGE_LENGTH_INNER_VIRTUAL_SHARP

    options = 0
    radius_mm = args.get("bend_radius_mm")
    radius = to_m(radius_mm) if radius_mm else 0.0
    if not radius_mm:
        options |= EDGE_FLANGE_USE_DEFAULT_RADIUS
    relief_name = args.get("relief_type")
    if relief_name:
        relief = RELIEF_TYPES[str(relief_name)]
        options |= EDGE_FLANGE_USE_RELIEF_RATIO
    else:
        relief = RELIEF_TYPES["rectangular"]
        options |= EDGE_FLANGE_USE_DEFAULT_RELIEF
    ratio = float(args.get("relief_ratio", 0.5))

    sketch_features: list[Any] = []
    sketches: list[Any] = []
    try:
        for edge in edges:
            sketch_feature, sketch = _draw_flange_profile(app, doc, edge, angle, bool(args.get("flip", False)), length)
            sketch_features.append(sketch_feature)
            sketches.append(sketch)
    except Exception:
        # The profiles of the earlier edges are useless without the flange.
        delete_features(doc, sketch_features)
        raise

    clear_selection(doc)
    try:
        feature, _ = _build(
            doc, "edge flange",
            lambda: feature_manager(doc).InsertSheetMetalEdgeFlange2(
                dispatch_array(edges), dispatch_array(sketches), options, angle, radius,
                position, length, relief, ratio, 0.0, 0.0, reference, pythoncom.Nothing,
            ),
            ("EdgeFlange",),
        )
    except Exception:
        # A COM error from the insert (bad argument, refused edge) is reported
        # as such; _build has removed what the call added, the profiles go here.
        delete_features(doc, sketch_features)
        raise
    if feature is None:
        # Leave no half-built profile sketches behind, and say whether that worked.
        profiles_removed = delete_features(doc, sketch_features)
        payload = _sheet_result(doc, None, "edge flange", edges=indices, length_mm=args["length_mm"], profiles_removed=profiles_removed)
        payload["message"] += (
            " Check that the edges are straight free edges of the sheet, that the flange does not run into "
            "existing material (try flip), and that no two edges belong to the same corner."
        )
        if not profiles_removed:
            payload["message"] += " The profile sketches could not all be removed; see list_sketches."
        return payload
    rename_feature(feature, args.get("name"))
    payload = _sheet_result(doc, feature, "edge flange", edges=indices, length_mm=args["length_mm"], angle_deg=args.get("angle_deg", 90))
    definition = _definition(feature)
    applied = None
    if definition is not None:
        position_names = {v: k for k, v in FLANGE_POSITIONS.items()}
        applied = {
            "angle_deg": round(to_deg(safe(definition, "BendAngle", 0.0) or 0.0), 6),
            "bend_radius_mm": round(to_mm(safe(definition, "BendRadius", 0.0) or 0.0), 6),
            "default_radius": bool(safe(definition, "UseDefaultBendRadius", False)),
            "position": position_names.get(int(safe(definition, "PositionType", 0) or 0)),
            "length_mm": round(to_mm(safe(definition, "OffsetDistance", 0.0) or 0.0), 6),
            "length_reference": int(safe(definition, "OffsetDimType", 0) or 0),
            "relief_ratio": safe(definition, "ReliefRatio"),
        }
        payload["data"]["flange"] = applied
    # Everything the call set is held against the readback; flip and the
    # relief type have no readback on 2016 and are left to the geometry.
    wanted = {
        "angle_deg": float(args.get("angle_deg", 90)),
        "bend_radius_mm": float(radius_mm) if radius_mm else None,
        "default_radius": not radius_mm,
        "position": str(args.get("position", "bend_outside")),
        "length_mm": float(args["length_mm"]),
        "length_reference": FLANGE_LENGTH_INNER_VIRTUAL_SHARP,
        "relief_ratio": ratio if relief_name else None,
    }
    mismatches = readback_mismatches(wanted, applied)
    if mismatches:
        payload["ok"] = False
        payload["message"] += f" SOLIDWORKS did not apply {'; '.join(mismatches)}."
    return payload


@tool(
    "sheet_metal_miter_flange",
    "Add a miter flange: a profile sketch swept along one or more connected edges of the sheet, with the "
    "corners mitred. The sketch is an open chain of lines on a plane perpendicular to the first edge, "
    "starting at that edge (draw it on a face or reference plane through the edge's end point; every "
    "line becomes a flange, every corner a bend). Put the edges in selection.edges.",
    {
        "sketch_name": _SKETCH_ARG,
        "selection": SELECTION_SCHEMA,
        "bend_radius_mm": {"type": "number", "exclusiveMinimum": 0, "description": "Defaults to the part's sheet metal radius."},
        "gap_mm": {"type": "number", "default": 0.5, "minimum": 0, "description": "Rip gap at the mitred corners."},
        "position": {"type": "string", "enum": sorted(FLANGE_POSITIONS), "default": "material_inside"},
        "trim_side_bends": {"type": "boolean", "default": True, "description": "Not read back on 2016; judge it by the geometry."},
        "start_offset_mm": {"type": "number", "default": 0, "minimum": 0, "description": "Leave this much of the edge free at its start."},
        "end_offset_mm": {"type": "number", "default": 0, "minimum": 0},
        "relief_type": {"type": "string", "enum": ["rectangular", "tear", "obround"], "description": "Defaults to the part's automatic relief."},
        "relief_ratio": {"type": "number", "default": 0.5},
        "name": {"type": "string"},
    },
    ["selection"],
)
def sheet_metal_miter_flange(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    _require_sheet_metal(doc)
    indices = list((args.get("selection") or {}).get("edges") or [])
    if not indices:
        return result(False, "sheet_metal_miter_flange needs selection.edges with at least one edge index from list_edges.")
    edges = _edge_objects(doc, indices)
    sketch = select_sketch_for_feature(doc, args.get("sketch_name"))
    for edge in edges:
        if not select_object(doc, edge, 0, True):
            return result(False, "Could not select one of the edges for the miter flange.")

    radius_mm = args.get("bend_radius_mm")
    relief_name = args.get("relief_type")
    feature, _ = _build(
        doc, "miter flange",
        lambda: feature_manager(doc).InsertSheetMetalMiterFlange(
            not radius_mm, to_m(radius_mm) if radius_mm else 0.0, to_m(args.get("gap_mm", 0.5)),
            relief_name is None, relief_name is not None, float(args.get("relief_ratio", 0.5)), 0.0, 0.0,
            RELIEF_TYPES[str(relief_name or "rectangular")],
            bool(args.get("trim_side_bends", True)),
            FLANGE_POSITIONS[str(args.get("position", "material_inside"))],
            to_m(args.get("start_offset_mm", 0)), to_m(args.get("end_offset_mm", 0)),
            pythoncom.Nothing,
        ),
        ("SMMiteredFlange",),
    )
    rename_feature(feature, args.get("name"))
    payload = _sheet_result(doc, feature, "miter flange", sketch=sketch, edges=indices)
    if feature is None:
        payload["message"] += (
            " The profile must be an open chain of lines on a plane perpendicular to the first edge, "
            "starting on that edge, and the edges must be straight and connected."
        )
        return payload
    definition = _definition(feature)
    applied = None
    if definition is not None:
        position_names = {v: k for k, v in FLANGE_POSITIONS.items()}
        relief_names = {v: k for k, v in RELIEF_TYPES.items()}
        applied = {
            "bend_radius_mm": round(to_mm(safe(definition, "BendRadius", 0.0) or 0.0), 6),
            "default_radius": bool(safe(definition, "UseDefaultBendRadius", False)),
            "gap_mm": round(to_mm(safe(definition, "GapDistance", 0.0) or 0.0), 6),
            "position": position_names.get(int(safe(definition, "PositionType", 0) or 0)),
            "start_offset_mm": round(to_mm(safe(definition, "StartOffset", 0.0) or 0.0), 6),
            "end_offset_mm": round(to_mm(safe(definition, "EndOffset", 0.0) or 0.0), 6),
            "relief_type": relief_names.get(int(safe(definition, "ReliefType", 0) or 0)),
            "relief_ratio": safe(definition, "ReliefRatio"),
        }
        payload["data"]["flange"] = applied
    wanted = {
        "bend_radius_mm": float(radius_mm) if radius_mm else None,
        "default_radius": not radius_mm,
        "gap_mm": float(args.get("gap_mm", 0.5)),
        "position": str(args.get("position", "material_inside")),
        "start_offset_mm": float(args.get("start_offset_mm", 0)),
        "end_offset_mm": float(args.get("end_offset_mm", 0)),
        "relief_type": str(relief_name) if relief_name else None,
        "relief_ratio": float(args.get("relief_ratio", 0.5)) if relief_name else None,
    }
    mismatches = readback_mismatches(wanted, applied)
    if mismatches:
        payload["ok"] = False
        payload["message"] += f" SOLIDWORKS did not apply {'; '.join(mismatches)}."
    return payload


@tool(
    "sheet_metal_hem",
    "Fold the edge of the sheet back on itself. closed and open hems take length_mm (open also gap_mm); "
    "rolled hems take angle_deg and radius_mm. position inside keeps the hem within the edge, "
    "outside adds it beyond. Put the edges in selection.edges.",
    {
        "selection": SELECTION_SCHEMA,
        "type": {"type": "string", "enum": sorted(HEM_TYPES), "default": "closed"},
        "position": {"type": "string", "enum": sorted(HEM_POSITIONS), "default": "inside"},
        "length_mm": {"type": "number", "exclusiveMinimum": 0, "default": 10},
        "gap_mm": {"type": "number", "minimum": 0, "default": 0.5},
        "angle_deg": {"type": "number", "exclusiveMinimum": 0, "default": 270},
        "radius_mm": {"type": "number", "exclusiveMinimum": 0, "default": 2},
        "reverse": {"type": "boolean", "default": False, "description": "Fold towards the other face."},
        "name": {"type": "string"},
    },
    ["selection"],
)
def sheet_metal_hem(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    _require_sheet_metal(doc)
    exit_active_sketch(doc)
    count = require_selection(doc, args["selection"])
    hem_type = HEM_TYPES[str(args.get("type", "closed"))]
    feature, _ = _build(
        doc, "hem",
        lambda: doc.InsertSheetMetalHem(
            hem_type, HEM_POSITIONS[str(args.get("position", "inside"))], bool(args.get("reverse", False)),
            to_m(args.get("length_mm", 10)), to_m(args.get("gap_mm", 0.5)),
            to_rad(args.get("angle_deg", 270)), to_m(args.get("radius_mm", 2)), 0.0,
        ),
        ("Hem",),
    )
    rename_feature(feature, args.get("name"))
    payload = _sheet_result(doc, feature, "hem", edges=count, type=str(args.get("type", "closed")))
    if feature is not None:
        definition = _definition(feature)
        applied = None
        if definition is not None:
            applied = {
                "length_mm": round(to_mm(safe(definition, "Length", 0.0) or 0.0), 6),
                "gap_mm": round(to_mm(safe(definition, "GapDistance", 0.0) or 0.0), 6),
                "angle_deg": round(to_deg(safe(definition, "Angle", 0.0) or 0.0), 6),
                "radius_mm": round(to_mm(safe(definition, "Radius", 0.0) or 0.0), 6),
            }
            payload["data"]["hem"] = applied
        # Only what the caller asked for is held against the readback; the
        # defaults SOLIDWORKS fills in for a hem type are its own business.
        wanted = {k: args.get(k) for k in ("length_mm", "gap_mm", "angle_deg", "radius_mm")}
        mismatches = readback_mismatches(wanted, applied)
        if mismatches:
            payload["ok"] = False
            payload["message"] += f" SOLIDWORKS did not apply {'; '.join(mismatches)}."
    else:
        payload["message"] += " Hems need a straight free edge; tear_drop and double were not accepted on SOLIDWORKS 2016."
    return payload


@tool(
    "sheet_metal_closed_corner",
    "Close the gap where two flanges meet at a corner by extending one of them. Put the planar end face "
    "of the flange to extend in selection.faces (the face that looks at the other flange). "
    "corner_type and gap_mm change the result after creation; the readback reports what was applied.",
    {
        "selection": SELECTION_SCHEMA,
        "corner_type": {"type": "string", "enum": sorted(CLOSED_CORNER_TYPES), "description": "butt, overlap or underlap. Defaults to the SOLIDWORKS default."},
        "gap_mm": {"type": "number", "minimum": 0, "description": "Gap left between the flanges."},
        "overlap_ratio": {"type": "number", "minimum": 0, "maximum": 1, "description": "For overlap/underlap: how far the extension reaches, 1 = full."},
        "open_bend_region": {"type": "boolean"},
        "name": {"type": "string"},
    },
    ["selection"],
)
def sheet_metal_closed_corner(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    _require_sheet_metal(doc)
    exit_active_sketch(doc)
    count = require_selection(doc, args["selection"])
    feature, _ = _build(doc, "closed corner", lambda: doc.InsertSheetMetalClosedCorner(), ("CornerFeat",))
    rename_feature(feature, args.get("name"))
    if feature is None:
        payload = _sheet_result(doc, None, "closed corner", faces=count)
        payload["message"] += " Select the planar end face of one flange where it meets the other flange."
        return payload

    wanted = {
        "CornerType": CLOSED_CORNER_TYPES[str(args["corner_type"])] if args.get("corner_type") else None,
        "GapDistance": to_m(args["gap_mm"]) if args.get("gap_mm") is not None else None,
        "OverlapUnderlapRatio": float(args["overlap_ratio"]) if args.get("overlap_ratio") is not None else None,
        "OpenBendRegion": bool(args["open_bend_region"]) if args.get("open_bend_region") is not None else None,
    }
    no_definition = False
    if any(v is not None for v in wanted.values()):
        definition = _definition(feature)
        if definition is None:
            no_definition = True
        else:
            try:
                definition.AccessSelections(doc, pythoncom.Nothing)
            except Exception:
                pass
            try:
                for member, wanted_value in wanted.items():
                    if wanted_value is not None:
                        setattr(definition, member, wanted_value)
                modified = _modify(feature, doc, definition)
            except Exception:
                # AccessSelections rolled the model back to before the feature;
                # without the release the part would stay in that state.
                _release_selection_access(definition)
                raise
            if not modified:
                _release_selection_access(definition)
    payload = _sheet_result(doc, feature, "closed corner", faces=count)
    if no_definition:
        payload["ok"] = False
        payload["message"] += " The corner's definition could not be read, so corner_type, gap_mm, overlap_ratio and open_bend_region were not applied."
    definition = _definition(feature)
    if definition is not None:
        applied = {
            "corner_type": {v: k for k, v in CLOSED_CORNER_TYPES.items()}.get(int(safe(definition, "CornerType", 0) or 0)),
            "gap_mm": round(to_mm(safe(definition, "GapDistance", 0.0) or 0.0), 6),
            "overlap_ratio": safe(definition, "OverlapUnderlapRatio"),
            "open_bend_region": bool(safe(definition, "OpenBendRegion", False)),
        }
        payload["data"]["corner"] = applied
        mismatches = []
        if wanted["CornerType"] is not None and applied["corner_type"] != args.get("corner_type"):
            mismatches.append("corner_type")
        if wanted["GapDistance"] is not None and abs(applied["gap_mm"] - float(args["gap_mm"])) > 1e-6:
            mismatches.append("gap_mm")
        if wanted["OverlapUnderlapRatio"] is not None and (
            applied["overlap_ratio"] is None
            or abs(float(applied["overlap_ratio"]) - wanted["OverlapUnderlapRatio"]) > 1e-6
        ):
            mismatches.append("overlap_ratio")
        if wanted["OpenBendRegion"] is not None and applied["open_bend_region"] != wanted["OpenBendRegion"]:
            mismatches.append("open_bend_region")
        if mismatches:
            payload["ok"] = False
            payload["message"] += f" SOLIDWORKS did not apply {', '.join(mismatches)}; see data.corner for what it kept."
    return payload


@tool(
    "sheet_metal_break_corner",
    "Round or chamfer the sharp corners of a flat sheet edge. Put the short corner edges (the ones "
    "across the thickness) or whole faces in selection; distance_mm is the fillet radius or chamfer distance.",
    {
        "selection": SELECTION_SCHEMA,
        "mode": {"type": "string", "enum": sorted(BREAK_CORNER_TYPES), "default": "fillet"},
        "distance_mm": {"type": "number", "exclusiveMinimum": 0},
        "name": {"type": "string"},
    },
    ["selection", "distance_mm"],
)
def sheet_metal_break_corner(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    _require_sheet_metal(doc)
    exit_active_sketch(doc)
    count = require_selection(doc, args["selection"])
    mode = str(args.get("mode", "fillet"))
    volume_before = _volume_mm3(doc)
    feature, _ = _build(
        doc, "break corner",
        lambda: doc.InsertSheetMetalBreakCorner(BREAK_CORNER_TYPES[mode], to_m(args["distance_mm"])),
        ("BreakCorner",),
    )
    rename_feature(feature, args.get("name"))
    payload = _sheet_result(doc, feature, "break corner", entities=count, mode=mode, distance_mm=args["distance_mm"])
    if feature is not None:
        definition = _definition(feature)
        if definition is not None:
            corners = int(safe(definition, "GetEntitiesCount", 0) or 0)
            payload["data"]["corners"] = corners
            if corners != count:
                payload["ok"] = False
                payload["message"] += f" The feature holds {corners} entities, {count} were selected."
        # A fillet or chamfer on a corner takes material away; a feature that
        # removed nothing broke no corner.
        if volume_before is not None and payload["data"].get("volume_mm3") is not None:
            removed = round(volume_before - payload["data"]["volume_mm3"], 4)
            payload["data"]["removed_mm3"] = removed
            if removed <= 0:
                payload["ok"] = False
                payload["message"] += " The break corner removed no material."
    return payload


@tool(
    "sheet_metal_corner_relief",
    "Cut a relief where two bends meet at a corner, so the flat pattern can be folded without tearing. "
    "Each corner is given by its two bend faces: the cylindrical faces (list_faces surface_type cylinder) "
    "of the two bends that meet there, outer or inner, as a pair in corners. size_mm is the side of a "
    "square relief, the length of an obround one, or the radius of a circular one; width_mm is the slot "
    "width of an obround relief or the fillet radius of a square one with filleted corners.",
    {
        "corners": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "properties": {"faces": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}},
                "required": ["faces"],
            },
            "description": "One entry per corner, each with the two bend face indices of that corner.",
        },
        "relief_type": {"type": "string", "enum": sorted(CORNER_RELIEF_TYPES), "default": "square"},
        "size_mm": {"type": "number", "exclusiveMinimum": 0, "default": 2},
        "width_mm": {"type": "number", "minimum": 0, "default": 0},
        "center_on_bend_lines": {"type": "boolean", "default": False},
        "ratio_to_thickness": {"type": "boolean", "default": False, "description": "Read size_mm and width_mm as multiples of the thickness."},
        "tangent_to_bend": {"type": "boolean", "default": False},
        "filleted_corners": {"type": "boolean", "default": False, "description": "Square relief only: round its corners with width_mm."},
        "narrow_corner": {"type": "boolean", "default": False},
        "name": {"type": "string"},
    },
    ["corners"],
)
def sheet_metal_corner_relief(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    _require_sheet_metal(doc)
    exit_active_sketch(doc)
    relief = CORNER_RELIEF_TYPES[str(args.get("relief_type", "square"))]
    ratio = bool(args.get("ratio_to_thickness", False))
    size = float(args.get("size_mm", 2)) if ratio else to_m(args.get("size_mm", 2))
    width = float(args.get("width_mm", 0)) if ratio else to_m(args.get("width_mm", 0))
    faces = iter_face_objects(doc)
    pairs = corner_face_pairs(args["corners"], len(faces))
    manager = feature_manager(doc)
    volume_before = _volume_mm3(doc)
    accepted = 0
    for pair in pairs:
        clear_selection(doc)
        for index in pair:
            if not select_object(doc, faces[index][0], 4, True):
                _finish_corner_relief_quietly(manager)
                raise RuntimeError(f"Could not select face {index}.")
        try:
            manager.AddCornerReliefCorner()
            if bool(manager.AddCornerReliefType(
                -1, relief, 0.0, size, width,
                bool(args.get("center_on_bend_lines", False)), ratio,
                bool(args.get("tangent_to_bend", False)), bool(args.get("filleted_corners", False)),
                bool(args.get("narrow_corner", False)),
            )):
                accepted += 1
        except Exception as exc:
            if not is_server_fault(exc):
                raise
            # Flat faces instead of bend faces raise here and define no corner.
            logger.info("Corner %s was not accepted as a bend corner: %s", pair, exc)
    clear_selection(doc)
    if accepted == 0:
        # The relief was begun with AddCornerReliefCorner; finishing it with no
        # accepted corner closes that build so the next call starts clean.
        _finish_corner_relief_quietly(manager)
        return result(False, "SOLIDWORKS accepted none of the corners. Each corner needs the two cylindrical bend faces that meet there.")
    feature, _ = _build(doc, "corner relief", lambda: manager.FinishCornerRelief(), ("CornerRelief",))
    rename_feature(feature, args.get("name"))
    # corners_accepted is what AddCornerReliefType answered, an API count; the
    # geometry gate is the removed volume below.
    payload = _sheet_result(doc, feature, "corner relief", corners_accepted=accepted, relief_type=str(args.get("relief_type", "square")))
    if feature is not None and volume_before is not None and payload["data"].get("volume_mm3") is not None:
        removed = round(volume_before - payload["data"]["volume_mm3"], 4)
        payload["data"]["removed_mm3"] = removed
        if removed <= 0:
            payload["ok"] = False
            payload["message"] += " The relief removed no material."
    if accepted < len(args["corners"]):
        payload["ok"] = False
        payload["message"] += f" {len(args['corners']) - accepted} corner(s) were not accepted and left out."
    return payload


# --------------------------------------------------------------------------
# Flat pattern
# --------------------------------------------------------------------------


@tool(
    "sheet_metal_flatten",
    "Show the sheet metal part flat (flat=true) or folded again (flat=false) by unsuppressing or "
    "suppressing its Flat-Pattern feature. Reports the bend state and the bounding box, so the flat "
    "size can be checked against the expected developed length. Fold the part back before adding features. "
    "Single-body sheet metal parts only; a multibody part (one Flat-Pattern per body) is refused.",
    {"flat": {"type": "boolean", "default": True}},
)
def sheet_metal_flatten(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    _require_sheet_metal(doc)
    exit_active_sketch(doc)
    flat = bool(args.get("flat", True))
    feature = _single_flat_pattern(doc)
    if feature is None:
        return result(False, "The part has no Flat-Pattern feature.")
    clear_selection(doc)
    if not bool(selectable(feature).Select2(False, 0)):
        return result(False, "Could not select the Flat-Pattern feature.")
    # Both take no arguments; value() handles the property-or-method ambiguity.
    accepted = bool(value(doc, "EditUnsuppress2" if flat else "EditSuppress2"))
    rebuild(doc)
    clear_selection(doc)
    now_flat = _is_flattened(doc)
    return result(
        now_flat == flat,
        ("Flattened the part." if flat else "Folded the part.") if now_flat == flat
        else f"SOLIDWORKS {'accepted' if accepted else 'refused'} the change but the part is {'flat' if now_flat else 'folded'}.",
        flat=now_flat,
        bend_state=_bend_state(doc),
        size_mm=_size_mm(doc),
        problems=whats_wrong(doc),
    )


@tool(
    "sheet_metal_info",
    "Read-only: the sheet metal parameters of the active part (thickness, bend radius, K-factor, relief), "
    "whether it is shown flat, and every sheet metal feature with its bends (angle, radius, direction).",
    {},
)
def sheet_metal_info(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    sheet = _sheet_metal_feature(doc)
    if sheet is None:
        return result(False, "The active part is not a sheet metal part.", sheet_metal=False)
    features: list[dict[str, Any]] = []
    for feature in iter_feature_objects(doc):
        type_name = str(feature_property(feature, "GetTypeName2", ""))
        kind = SHEET_METAL_FEATURE_TYPES.get(type_name)
        if kind is None:
            continue
        entry: dict[str, Any] = {
            "name": str(feature_property(feature, "Name", "")),
            "kind": kind,
            "suppressed": bool(safe(feature, "IsSuppressed", False)),
        }
        bends = []
        try:
            sub = value(feature, "GetFirstSubFeature")
            while sub is not None:
                if str(feature_property(sub, "GetTypeName2", "")) == "OneBend":
                    definition = _definition(sub)
                    bend = {"name": str(feature_property(sub, "Name", ""))}
                    if definition is not None:
                        bend["angle_deg"] = round(to_deg(safe(definition, "BendAngle", 0.0) or 0.0), 4)
                        bend["radius_mm"] = round(to_mm(safe(definition, "BendRadius", 0.0) or 0.0), 6)
                        bend["down"] = bool(safe(definition, "BendDown", False))
                    bends.append(bend)
                sub = value(sub, "GetNextSubFeature")
        except Exception:
            pass
        if bends:
            entry["bends"] = bends
        features.append(entry)
    return result(
        True,
        "Read the sheet metal state.",
        sheet_metal=True,
        parameters=_sheet_metal_parameters(doc),
        flat=_is_flattened(doc),
        bend_state=_bend_state(doc),
        size_mm=_size_mm(doc),
        features=features,
    )


@tool(
    "export_flat_pattern",
    "Write the flat pattern of the active sheet metal part as DXF or DWG under the designated outputs "
    "folder, without any dialog. The part must have been saved (save_document) because SOLIDWORKS "
    "exports by model path. Bend lines are included by default. For a DXF the result summarises the "
    "entities, bend-line count and extents so the file can be checked without opening it. Single-body "
    "sheet metal parts only; a multibody part (one Flat-Pattern per body) is refused.",
    {
        "path": {"type": "string", "description": "Target file, .dxf or .dwg; relative paths land in the outputs folder."},
        "overwrite": {"type": "boolean", "default": False},
        "bend_lines": {"type": "boolean", "default": True},
        "sketches": {"type": "boolean", "default": False, "description": "Include the model's sketches (the flange profile sketches among them)."},
        "hidden_edges": {"type": "boolean", "default": False},
        "library_features": {"type": "boolean", "default": False},
        "forming_tools": {"type": "boolean", "default": False},
        "merge_coplanar_faces": {"type": "boolean", "default": False},
        "flip_x": {"type": "boolean", "default": False},
        "flip_y": {"type": "boolean", "default": False},
    },
    ["path"],
)
def export_flat_pattern(args: dict[str, Any]) -> dict[str, Any]:
    from .sw_file import validated_output_path

    _, doc = require_part()
    _require_sheet_metal(doc)
    if _single_flat_pattern(doc) is None:
        return result(False, "The part has no Flat-Pattern feature to export.")
    model_path = str(value(doc, "GetPathName") or "")
    if not model_path:
        return result(False, "The part has no saved path; save it with save_document first, then export the flat pattern.")
    output = validated_output_path(str(args["path"]), FLAT_PATTERN_EXTENSIONS, bool(args.get("overwrite", False)))
    if output.exists():
        # With overwrite the old file goes first, so that the existence check
        # below sees what this export wrote and not what an earlier one did.
        output.unlink()
    exit_active_sketch(doc)
    clear_selection(doc)

    options = export_options(args)
    alignment = double_array([0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1])
    exported = bool(doc.ExportToDWG2(
        str(output), model_path, EXPORT_SHEET_METAL, True, alignment,
        bool(args.get("flip_x", False)), bool(args.get("flip_y", False)), options, null_variant(),
    ))
    exists = output.is_file()
    if not exported or not exists:
        return result(
            False,
            "SOLIDWORKS did not export the flat pattern." if not exported else f"SOLIDWORKS reported success but {output} does not exist.",
            path=str(output), options=options,
        )
    payload = result(True, f"Exported the flat pattern to {output}.", path=str(output), bytes=output.stat().st_size, options=options)
    if output.suffix.lower() == ".dxf":
        try:
            payload["data"]["dxf"] = summarize_dxf(output)
        except Exception as exc:
            payload["data"]["dxf"] = {"error": str(exc)}
    return payload
