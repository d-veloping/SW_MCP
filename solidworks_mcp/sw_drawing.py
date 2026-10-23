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

"""Drawing documents: sheets, views, and annotation.

Two different things get called "auto-dimension" in SOLIDWORKS, and they are
not interchangeable, so both are exposed:

* ``insert_model_annotations`` imports the dimensions the model already carries
  (Insert > Model Items).  Use this when the part was modelled with the
  dimensions you want to see on the drawing.
* ``auto_dimension_view`` runs the DimXpert scheme (baseline / ordinate /
  chain) over a view's geometry and invents dimensions.  Use this when the
  model has nothing worth importing.

Sheet coordinates are millimetres from the lower-left corner of the sheet.

Geometry sketched while a view is active belongs to that view's own sketch,
whose coordinates are not sheet coordinates: a point sits at
``(sheet - view position) / view scale`` (measured on 2016 SP3, d-veloping/ClauSW#157).
Section lines and detail circles are converted that way before they are drawn;
drawn at sheet coordinates they land beside the part, and SOLIDWORKS still
creates a "section view" that cuts nothing (issue #2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .sw_core import (
    active_document,
    as_list,
    byref_long,
    call_versioned,
    clear_selection,
    document_info,
    document_type,
    extension,
    feature_property,
    flag_methods,
    logger,
    rebuild,
    result,
    running_app,
    safe,
    sketch_manager,
    to_m,
    to_mm,
    tool,
    value,
    nothing,
)
from .sw_file import _identity, _new_document


# swDwgPaperSizes_e
PAPER_SIZES = {
    "A": 0, "A_portrait": 1, "B": 2, "C": 3, "D": 4, "E": 5,
    "A4": 6, "A4_portrait": 7, "A3": 8, "A2": 9, "A1": 10, "A0": 11,
    "custom": 12,
}

# swDrawingViewTypes_e
VIEW_TYPES = {
    1: "sheet", 2: "section", 3: "detail", 4: "projected", 5: "auxiliary",
    6: "standard", 7: "named", 8: "relative", 9: "detached", 10: "alternate_position",
}

# swDisplayMode_e: the Mode IView.SetDisplayMode3 takes and IView.GetDisplayMode2
# returns -- one enum both ways, measured on 2016 SP3 (issue #4).  Edges only
# matters for shaded (3) and reads back through GetDisplayEdgesInShadedMode.
# SetDisplayMode3 also returns True for modes it does not have (4 and 5 came
# back as 0 and 1), so a mode counts as applied only once it reads back.
DISPLAY_MODES = {
    "wireframe": (0, False),
    "hidden_lines_visible": (1, False),
    "hidden_lines_removed": (2, False),
    "shaded": (3, False),
    "shaded_with_edges": (3, True),
}
# Accepted for existing callers, not advertised.
DISPLAY_MODE_ALIASES = {"hidden_lines_grey": "hidden_lines_visible"}
DISPLAY_MODE_SCHEMA = {
    "type": "string",
    "enum": sorted(DISPLAY_MODES) + sorted(DISPLAY_MODE_ALIASES),
    "description": "Display style of the view, checked by reading it back. hidden_lines_visible draws hidden "
                   "edges dashed, so bores and steps show without a section. (hidden_lines_grey is a legacy "
                   "alias of hidden_lines_visible.)",
}

# swInsertAnnotation_e, the flags worth exposing on a mechanical drawing.
ANNOTATION_TYPES = {
    # swInsertDimensions on its own imports nothing: SOLIDWORKS only hands over
    # dimensions when one of the marked/not-marked flags is present. "dimensions"
    # therefore means all three, which is what asking for dimensions implies.
    "dimensions": 8 | 32768 | 524288,
    "marked_for_drawing": 32768,
    "not_marked_for_drawing": 524288,
    "hole_callouts": 1048576,
    "hole_wizard_profile": 65536,
    "hole_wizard_location": 131072,
    "notes": 64,
    "geometric_tolerances": 32,
    "datums": 2,
    "surface_finish": 128,
    "cosmetic_threads": 1,
    "axes": 512,
    "toleranced_dimensions": 16777216,
}

# swAutodimScheme_e / placement enums
AUTODIM_SCHEMES = {"baseline": 1, "ordinate": 2, "chain": 3, "centerline": 4}
AUTODIM_ENTITIES = {"all": 1, "selected": 2, "preselected": 0}
HORIZONTAL_PLACEMENT = {"below": -1, "above": 1}
VERTICAL_PLACEMENT = {"left": -1, "right": 1}

# swCenterMarkStyle_e
CENTER_MARK_STYLES = {"single": 2, "linear": 3, "circular": 4}

_DRAWING_METHODS = (
    "NewSheet3", "SetupSheet5", "SetupSheet4", "ActivateSheet", "ActivateView",
    "CreateDrawViewFromModelView3", "Create3rdAngleViews2", "Create1stAngleViews2",
    "CreateUnfoldedViewAt3", "CreateSectionViewAt5", "CreateDetailViewAt4", "CreateDetailViewAt3",
    "InsertModelAnnotations3", "AutoDimension", "InsertCenterMark3", "InsertCenterLine2",
    "AutoInsertCenterMarks2", "SelectEntity",
    "GetSheetNames", "GetFirstView", "GetCurrentSheet",
)


def require_drawing() -> tuple[Any, Any]:
    app, doc = active_document()
    if document_type(doc) != 3:
        raise RuntimeError("This tool requires an active drawing document.")
    return app, flag_methods(doc, *_DRAWING_METHODS)


def _ensure_model_open(app: Any, model: str) -> None:
    """Load the model if it is not already open.

    CreateDrawViewFromModelView3 needs it in memory, and the localized standard
    view names can only be read off an open document.
    """
    for doc in as_list(value(app, "GetDocuments")):
        try:
            if str(value(doc, "GetPathName") or "").lower() == model.lower():
                return
        except Exception:
            continue
    doc_type = {".sldprt": 1, ".sldasm": 2}.get(Path(model).suffix.lower(), 1)
    errors, warnings = byref_long(0), byref_long(0)
    # Opening a document activates it, which would leave the drawing tools
    # looking at a part, so put the drawing back in front afterwards.
    previous = ""
    try:
        current = app.ActiveDoc
        previous = str(value(current, "GetTitle")) if current is not None else ""
    except Exception:
        previous = ""
    try:
        # swOpenDocOptions_Silent = 1
        app.OpenDoc6(model, doc_type, 1, "", errors, warnings)
    except Exception:
        logger.info("Could not silently open %s for the drawing view.", model)
    if previous:
        try:
            flag_methods(app, "ActivateDoc3").ActivateDoc3(previous, True, 0, byref_long(0))
        except Exception:
            logger.info("Could not re-activate %s after loading the model.", previous)


def _open_model_path(app: Any, model_path: str | None) -> str:
    """Resolve which model a view should be built from.

    Defaults to the only open part or assembly, which is what the caller means
    almost every time.
    """
    if model_path:
        path = Path(str(model_path)).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"No such model file: {path}")
        _ensure_model_open(app, str(path))
        return str(path)

    candidates = []
    for doc in as_list(value(app, "GetDocuments")):
        if int(value(doc, "GetType")) in (1, 2):
            path = str(value(doc, "GetPathName") or "")
            if path:
                candidates.append(path)
    if not candidates:
        raise RuntimeError(
            "No saved part or assembly is open to draw. Save the model first, "
            "or pass model_path explicitly."
        )
    if len(candidates) > 1:
        raise RuntimeError(f"Several models are open; pass model_path. Candidates: {candidates}")
    return candidates[0]


def _iter_views(doc: Any) -> list[Any]:
    """The current sheet and its views, in SOLIDWORKS' own order.

    GetFirstView returns the current sheet itself, and its 'next' chain walks
    the views on that sheet only (measured on 2016 SP3 with a second sheet,
    d-veloping/ClauSW#157); _views_of_every_sheet reads all sheets.
    """
    views: list[Any] = []
    try:
        node = value(doc, "GetFirstView")
    except Exception:
        return views
    while node is not None:
        views.append(node)
        node = safe(node, "GetNextView")
    return views


def _is_sheet(view: Any) -> bool:
    return int(safe(view, "Type", 1) or 1) == 1


def _display_mode(view: Any) -> str | None:
    """The display style a view actually has, by the names of DISPLAY_MODES."""
    mode = safe(view, "GetDisplayMode2")
    if mode is None:
        return None
    mode = int(mode)
    if mode == 3:
        return "shaded_with_edges" if bool(safe(view, "GetDisplayEdgesInShadedMode", False)) else "shaded"
    return {0: "wireframe", 1: "hidden_lines_visible", 2: "hidden_lines_removed"}.get(mode, f"mode_{mode}")


def _set_display_mode(view: Any, name: str) -> dict[str, Any]:
    """Apply a display style and report whether it reads back."""
    wanted = DISPLAY_MODE_ALIASES.get(name, name)
    outcome: dict[str, Any] = {"view": str(safe(view, "GetName2", "") or ""), "requested": wanted}
    if wanted not in DISPLAY_MODES:
        outcome.update(actual=_display_mode(view), ok=False)
        return outcome
    mode, edges = DISPLAY_MODES[wanted]
    try:
        flag_methods(view, "SetDisplayMode3").SetDisplayMode3(False, mode, False, edges)
    except Exception:
        logger.info("SetDisplayMode3 failed on view %s", outcome["view"], exc_info=True)
    outcome["actual"] = _display_mode(view)
    outcome["ok"] = outcome["actual"] == wanted
    return outcome


def _display_failure(outcomes: list[dict[str, Any]]) -> str:
    wrong = [f"{o['view']} is {o['actual']}" for o in outcomes if not o["ok"]]
    return f"SOLIDWORKS did not apply display_mode '{outcomes[0]['requested']}': " + ", ".join(wrong) + "."


def _view_entry(view: Any, index: int) -> dict[str, Any]:
    entry: dict[str, Any] = {"index": index, "name": str(safe(view, "GetName2", "") or "")}
    kind = safe(view, "Type")
    if kind is not None:
        entry["type"] = VIEW_TYPES.get(int(kind), f"type_{int(kind)}")
        if int(kind) != 1:
            mode = _display_mode(view)
            if mode is not None:
                entry["display_mode"] = mode
            outline = safe(view, "GetOutline")
            if outline is not None and len(outline) >= 4:
                entry["outline_mm"] = [round(to_mm(c), 4) for c in list(outline)[:4]]
            hatches = safe(view, "GetFaceHatchCount")
            if hatches is not None:
                entry["face_hatches"] = int(hatches)
            entry["section_lines"] = len(as_list(safe(view, "GetSectionLines")))
    position = safe(view, "Position")
    if position is not None and len(position) >= 2:
        entry["position_mm"] = [round(to_mm(position[0]), 4), round(to_mm(position[1]), 4)]
    scale = safe(view, "ScaleDecimal")
    if scale:
        entry["scale"] = round(float(scale), 6)
    for member in ("GetDimensionCount4", "GetDimensionCount3", "GetDimensionCount2"):
        count = safe(view, member)
        if count is not None:
            entry["dimensions"] = int(count)
            break
    referenced = str(safe(view, "GetReferencedModelName", "") or "")
    if referenced:
        entry["model"] = referenced
    return entry


# --------------------------------------------------------------------------
# Documents and sheets
# --------------------------------------------------------------------------


@tool(
    "create_drawing",
    "Create a new drawing document from the configured default template, optionally sized and "
    "scaled. Then use insert_standard_views or insert_model_view to place views. The sheet is set up on the new "
    "drawing itself, also when it is not the active document. Succeeds only while the new drawing is still the "
    "active document at the end; otherwise it fails with the drawing in `document` and the document SOLIDWORKS made "
    "active in `active_document`, and never raises because of a switch. A switch after this check is not reported.",
    {
        "paper_size": {"type": "string", "enum": sorted(PAPER_SIZES), "description": "Override the template's paper size."},
        "scale_numerator": {"type": "number", "default": 1},
        "scale_denominator": {"type": "number", "default": 1},
        "first_angle": {"type": "boolean", "description": "True for first-angle (ISO/GB) projection, false for third-angle."},
    },
)
def create_drawing(args: dict[str, Any]) -> dict[str, Any]:
    created, raw = _new_document("drawing")
    if raw is None:
        return created
    # work on the drawing that was created, never on whatever ActiveDoc reports by now: right after open_document of
    # a part, SOLIDWORKS can make that part active again a moment later (ClauSW #115). The sheet is set up on this
    # object even when the drawing never became active, so a caller that brings it back gets the sheet it asked for.
    doc = flag_methods(raw, *_DRAWING_METHODS)
    own = (created["data"]["document"]["title"], created["data"]["document"]["path"])

    if args.get("paper_size") or args.get("first_angle") is not None or args.get("scale_numerator"):
        sheet = value(doc, "GetCurrentSheet")
        name = str(safe(sheet, "GetName", "") or "")
        size = PAPER_SIZES.get(str(args.get("paper_size", "")), int(safe(sheet, "GetSize", 8) or 8))
        try:
            doc.SetupSheet5(
                name, size, 12 if size == 12 else size,
                float(args.get("scale_numerator", 1)), float(args.get("scale_denominator", 1)),
                bool(args.get("first_angle", True)), "", 0.0, 0.0, "", True,
            )
        except Exception as exc:
            logger.info("Sheet setup was declined: %s", exc)
    # the final check also runs when the wait for activation ran out: SOLIDWORKS may finish a late activation during the
    # sheet setup, and only the state at the end counts
    active = running_app().ActiveDoc
    identity = _identity(active)
    if identity != own:
        shown = identity[0] if identity else "none"
        message = (f"Created a new drawing '{own[0]}', but '{shown}' became active again after the drawing was "
                   "created; nothing should be done in either." if created.get("ok")
                   else f"Created a new drawing '{own[0]}', but it is not the active document at the end ('{shown}' "
                   "is); nothing should be done in either.")
        return result(
            False, message, document=document_info(doc), activated=False, wait_ms=created["data"].get("wait_ms"),
            active_document=None if active is None else document_info(active),
        )
    return result(True, "Created a new drawing." if created.get("ok") else
                  "Created a new drawing; it became active only after the check in new_document.",
                  document=document_info(doc), sheets=_sheet_names(doc), activated=True,
                  wait_ms=created["data"].get("wait_ms"))


def _sheet_names(doc: Any) -> list[str]:
    return [str(n) for n in as_list(safe(doc, "GetSheetNames"))]


@tool("list_sheets", "Read-only: list the sheets of the active drawing, with the active one marked.", {})
def list_sheets(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    current = value(doc, "GetCurrentSheet")
    active = str(safe(current, "GetName", "") or "")
    sheets = []
    for name in _sheet_names(doc):
        sheets.append({"name": name, "active": name == active})
    properties = safe(current, "GetProperties2")
    payload: dict[str, Any] = {"sheets": sheets, "active_sheet": active}
    if properties is not None and len(properties) >= 6:
        # [paper size, template, scale1, scale2, first angle, width, height]
        payload["scale"] = [float(properties[2]), float(properties[3])]
        payload["first_angle"] = bool(properties[4])
        if len(properties) >= 7:
            payload["sheet_size_mm"] = [round(to_mm(properties[5]), 2), round(to_mm(properties[6]), 2)]
    return result(True, f"Read {len(sheets)} sheets.", **payload)


@tool(
    "add_sheet",
    "Add a sheet to the active drawing.",
    {
        "name": {"type": "string"},
        "paper_size": {"type": "string", "enum": sorted(PAPER_SIZES), "default": "A3"},
        "scale_numerator": {"type": "number", "default": 1},
        "scale_denominator": {"type": "number", "default": 1},
        "first_angle": {"type": "boolean", "default": True},
    },
)
def add_sheet(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    name = str(args.get("name") or f"Sheet{len(_sheet_names(doc)) + 1}")
    size = PAPER_SIZES[str(args.get("paper_size", "A3"))]
    ok = bool(
        doc.NewSheet3(
            name, size, size,
            float(args.get("scale_numerator", 1)), float(args.get("scale_denominator", 1)),
            bool(args.get("first_angle", True)), "", 0.0, 0.0, "",
        )
    )
    return result(ok, f"Added sheet '{name}'." if ok else "SOLIDWORKS did not add the sheet.", sheets=_sheet_names(doc))


@tool(
    "activate_sheet",
    "Make a sheet the active one.",
    {"name": {"type": "string"}},
    ["name"],
)
def activate_sheet(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    name = str(args["name"])
    ok = bool(doc.ActivateSheet(name))
    return result(ok, f"Activated sheet '{name}'." if ok else f"No sheet named '{name}'.", sheets=_sheet_names(doc))


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


@tool(
    "insert_standard_views",
    "Place the standard set of views (front, top, side, isometric) for a model in one call, "
    "laid out by projection angle. first_angle follows ISO/GB convention.",
    {
        "model_path": {"type": "string", "description": "Defaults to the only open saved part or assembly."},
        "first_angle": {"type": "boolean", "default": True},
        "display_mode": {**DISPLAY_MODE_SCHEMA, "description": "Applied to the views this call places. "
                         + DISPLAY_MODE_SCHEMA["description"]},
    },
)
def insert_standard_views(args: dict[str, Any]) -> dict[str, Any]:
    app, doc = require_drawing()
    model = _open_model_path(app, args.get("model_path"))
    first_angle = bool(args.get("first_angle", True))
    before = {str(safe(v, "GetName2", "")) for v in _iter_views(doc) if not _is_sheet(v)}
    ok = bool(doc.Create1stAngleViews2(model) if first_angle else doc.Create3rdAngleViews2(model))
    rebuild(doc)
    message = f"Placed the standard views for {Path(model).name}." if ok else "SOLIDWORKS did not place the standard views."
    display = None
    if ok and args.get("display_mode"):
        placed = [v for v in _iter_views(doc) if not _is_sheet(v) and str(safe(v, "GetName2", "")) not in before]
        display = [_set_display_mode(v, str(args["display_mode"])) for v in placed]
        rebuild(doc)
        if not display or not all(o["ok"] for o in display):
            ok = False
            message = _display_failure(display) if display else "No new view to apply display_mode to."
    views = [_view_entry(v, i) for i, v in enumerate(_iter_views(doc))]
    data: dict[str, Any] = {"model": model, "views": views}
    if display is not None:
        data["display"] = display
    return result(ok, message, **data)


@tool(
    "insert_model_view",
    "Place one named orientation of a model at a point on the sheet. Position is millimetres from "
    "the sheet's lower-left corner.",
    {
        "view": {
            "type": "string",
            "enum": ["front", "back", "left", "right", "top", "bottom",
                     "isometric", "trimetric", "dimetric", "normal_to", "current"],
            "default": "front",
        },
        "x_mm": {"type": "number"},
        "y_mm": {"type": "number"},
        "model_path": {"type": "string", "description": "Defaults to the only open saved part or assembly."},
        "scale": {"type": "number", "description": "Override the sheet scale for this view, e.g. 0.25 for 1:4."},
        "display_mode": DISPLAY_MODE_SCHEMA,
    },
    ["x_mm", "y_mm"],
)
def insert_model_view(args: dict[str, Any]) -> dict[str, Any]:
    app, doc = require_drawing()
    model = _open_model_path(app, args.get("model_path"))
    wanted = str(args.get("view", "front"))
    resolved = _model_view_name(app, model, wanted)
    view = doc.CreateDrawViewFromModelView3(
        model, resolved, to_m(args["x_mm"]), to_m(args["y_mm"]), 0.0
    )
    if view is None:
        return result(
            False,
            f"SOLIDWORKS did not place the {wanted} view (resolved to '{resolved}'). "
            f"Check that the model is open and the point is on the sheet.",
            model=model,
            resolved_view_name=resolved,
        )
    display = _apply_view_options(view, args)
    rebuild(doc)
    if display is not None and not display["ok"]:
        return result(False, f"Placed the {wanted} view, but " + _display_failure([display]),
                      view=_view_entry(view, -1), model=model, display=[display])
    return result(True, f"Placed the {wanted} view.", view=_view_entry(view, -1), model=model)


def _model_view_name(app: Any, model: str, wanted: str) -> str:
    """Resolve a standard orientation to the name this install actually uses.

    A localized SOLIDWORKS translates them — "*Isometric" is "*等轴测" on a
    Chinese install — and CreateDrawViewFromModelView3 silently returns nothing
    for a name it does not recognise.  GetModelViewNames reports them in a
    fixed order, so ask the model rather than hard-coding English.
    """
    english = {
        "normal_to": "*Normal To", "front": "*Front", "back": "*Back",
        "left": "*Left", "right": "*Right", "top": "*Top", "bottom": "*Bottom",
        "isometric": "*Isometric", "trimetric": "*Trimetric", "dimetric": "*Dimetric",
        "current": "*Current",
    }
    order = ["normal_to", "front", "back", "left", "right", "top", "bottom",
             "isometric", "trimetric", "dimetric"]
    if wanted not in order:
        return english.get(wanted, wanted)

    for doc in as_list(value(app, "GetDocuments")):
        try:
            if str(value(doc, "GetPathName") or "").lower() != model.lower():
                continue
            names = [str(n) for n in as_list(value(doc, "GetModelViewNames"))]
        except Exception:
            continue
        index = order.index(wanted)
        if len(names) > index:
            return names[index]
    return english[wanted]


def _apply_view_options(view: Any, args: dict[str, Any]) -> dict[str, Any] | None:
    """Scale and display style of one view.  Returns the display outcome of
    _set_display_mode when a display_mode was asked for, else None."""
    if args.get("scale"):
        try:
            view.ScaleDecimal = float(args["scale"])
        except Exception:
            logger.info("Could not override the view scale.")
    mode = args.get("display_mode")
    return _set_display_mode(view, str(mode)) if mode else None


@tool(
    "insert_projected_view",
    "Project a new view off an existing one. Name the parent and a direction and the placement is "
    "worked out for you; pass x_mm/y_mm instead to place it exactly. Millimetres.",
    {
        "parent_view": {"type": "string", "description": "View to project from. Defaults to the last view placed."},
        "direction": {"type": "string", "enum": ["right", "left", "up", "down"], "default": "right"},
        "offset_mm": {"type": "number", "default": 80, "description": "How far from the parent to place it."},
        "x_mm": {"type": "number", "description": "Exact placement, overriding direction/offset."},
        "y_mm": {"type": "number"},
        "not_aligned": {"type": "boolean", "default": False, "description": "Break alignment with the parent view."},
        "display_mode": DISPLAY_MODE_SCHEMA,
    },
)
def insert_projected_view(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    parent_name = args.get("parent_view")
    parent = None
    for view in _iter_views(doc):
        if int(safe(view, "Type", 1) or 1) == 1:
            continue
        if parent_name:
            if str(safe(view, "GetName2", "")) == str(parent_name):
                parent = view
                break
        else:
            parent = view
    if parent is None:
        return result(False, "No parent drawing view to project from. Place a view first.")

    parent_name = str(safe(parent, "GetName2", ""))
    position = safe(parent, "Position") or (0.0, 0.0)
    if args.get("x_mm") is not None and args.get("y_mm") is not None:
        x, y = to_m(args["x_mm"]), to_m(args["y_mm"])
    else:
        offset = to_m(args.get("offset_mm", 80))
        dx, dy = {
            "right": (offset, 0.0), "left": (-offset, 0.0),
            "up": (0.0, offset), "down": (0.0, -offset),
        }[str(args.get("direction", "right"))]
        x, y = float(position[0]) + dx, float(position[1]) + dy

    # The parent has to be the active view, and creating a view activates the
    # new one, so re-activate before every projection.
    doc.ActivateView(parent_name)
    view = doc.CreateUnfoldedViewAt3(x, y, 0.0, bool(args.get("not_aligned", False)))
    if view is None:
        rebuild(doc)
        doc.ActivateView(parent_name)
        view = doc.CreateUnfoldedViewAt3(x, y, 0.0, bool(args.get("not_aligned", False)))
    if view is None:
        return result(
            False,
            f"SOLIDWORKS did not project a view from '{parent_name}'. In practice only the first "
            f"projection off a given parent succeeds through this API; for a full set of "
            f"orthographic views use insert_standard_views, which places them in one call.",
            parent=parent_name,
            point_mm=[round(to_mm(x), 2), round(to_mm(y), 2)],
        )
    display = _apply_view_options(view, {"display_mode": args.get("display_mode")})
    rebuild(doc)
    if display is not None and not display["ok"]:
        return result(False, f"Projected a view from '{parent_name}', but " + _display_failure([display]),
                      parent=parent_name, view=_view_entry(view, -1), display=[display])
    return result(True, f"Projected a view from '{parent_name}'.", parent=parent_name, view=_view_entry(view, -1))


def _find_view(doc: Any, name: str | None) -> Any | None:
    """The model view called `name`, or the last one placed when no name is given."""
    found = None
    for view in _iter_views(doc):
        if _is_sheet(view):
            continue
        if name:
            if str(safe(view, "GetName2", "")) == str(name):
                return view
        else:
            found = view
    return found


def _view_scale(view: Any) -> float:
    return float(safe(view, "ScaleDecimal", 1.0) or 1.0)


def _sketch_point(view: Any, x_mm: float, y_mm: float) -> tuple[float, float]:
    """A sheet point (mm) in the coordinates of the view's own sketch (metres)."""
    position = list(safe(view, "Position") or (0.0, 0.0))
    scale = _view_scale(view)
    return (to_m(x_mm) - float(position[0])) / scale, (to_m(y_mm) - float(position[1])) / scale


def _model_to_sheet(view: Any) -> dict[str, Any] | None:
    """IView.ModelToViewTransform as rotation (row-major, as ArrayData stores it), translation and scale."""
    data = safe(safe(view, "ModelToViewTransform"), "ArrayData")
    if data is None or len(data) < 13:
        return None
    data = [float(c) for c in data]
    return {"rotation": [round(c, 9) for c in data[0:9]],
            "translation_mm": [round(to_mm(c), 6) for c in data[9:12]], "scale": round(data[12], 9)}


def _sheet_point(view: Any, point_mm: list[float]) -> tuple[float, float]:
    """Where a model point (mm) appears on the sheet (mm): sheet = p·R·s + t."""
    transform = _model_to_sheet(view)
    if transform is None:
        raise RuntimeError("SOLIDWORKS did not report the view's model transform.")
    r, t, s = transform["rotation"], transform["translation_mm"], transform["scale"]
    p = [float(c) for c in point_mm]
    return (s * (r[0] * p[0] + r[3] * p[1] + r[6] * p[2]) + t[0],
            s * (r[1] * p[0] + r[4] * p[1] + r[7] * p[2]) + t[1])


def _delete_selected(doc: Any, name: str, kind: str) -> bool:
    """Select one object by name and delete it; False when it cannot be selected or SOLIDWORKS refuses, never raises."""
    try:
        clear_selection(doc)
        if not bool(extension(doc).SelectByID2(name, kind, 0.0, 0.0, 0.0, False, 0, nothing(), 0)):
            return False
        return bool(flag_methods(extension(doc), "DeleteSelection2").DeleteSelection2(0))
    except Exception:
        logger.info("Could not delete %s '%s'.", kind, name, exc_info=True)
        return False
    finally:
        clear_selection(doc)


def _delete_view(doc: Any, name: str) -> bool:
    return _delete_selected(doc, name, "DRAWINGVIEW")


def _section_line_counts(doc: Any) -> dict[str, int]:
    """How many cutting lines each view of the active sheet carries."""
    return {str(safe(v, "GetName2", "") or ""): len(as_list(safe(v, "GetSectionLines")))
            for v in _iter_views(doc) if not _is_sheet(v)}


def _section_labels(doc: Any) -> set[str]:
    return {str(safe(line, "GetLabel", "") or "") for v in _iter_views(doc) if not _is_sheet(v)
            for line in as_list(safe(v, "GetSectionLines"))} - {""}


def _new_section_lines(doc: Any, before: dict[str, int]) -> tuple[str | None, list[Any]]:
    """The view that gained cutting lines since `before` (counted, so equal names cannot hide a new line), and those
    lines: SOLIDWORKS appends a new line to the parent's list."""
    for view in _iter_views(doc):
        if _is_sheet(view):
            continue
        name = str(safe(view, "GetName2", "") or "")
        lines = as_list(safe(view, "GetSectionLines"))
        if len(lines) > before.get(name, 0):
            return name, lines[before.get(name, 0):]
    return None, []


def _delete_section_line(doc: Any, line: Any) -> bool:
    name = str(safe(line, "GetName", "") or "")
    return bool(name) and _delete_selected(doc, name, "SECTIONLINE")


def _delete_entity(doc: Any, entity: Any) -> bool:
    """Select one sketch entity directly and delete it; False when SOLIDWORKS refuses, never raises."""
    try:
        clear_selection(doc)
        from .sw_core import select_object

        if not select_object(doc, entity, mark=0, append=False):
            return False
        return bool(flag_methods(extension(doc), "DeleteSelection2").DeleteSelection2(0))
    except Exception:
        logger.info("Could not delete the reference geometry drawn for a section.", exc_info=True)
        return False
    finally:
        clear_selection(doc)


def _draw_in_view(doc: Any, parent_view: str | None, draw: Any) -> tuple[str, Any]:
    """Activate a view, draw one entity inside it, and leave it selected.

    Sketch geometry in a drawing belongs to whichever view is active, so the
    activate/draw/select dance has to happen together or the entity lands on
    the sheet instead of in the view.  `draw` gets the sketch manager and the
    view, to convert sheet coordinates with _sketch_point.
    """
    parent = _find_view(doc, parent_view)
    if parent is None:
        raise RuntimeError("No parent drawing view. Place a view before sectioning or detailing it.")

    name = str(safe(parent, "GetName2", ""))
    if not bool(doc.ActivateView(name)):
        raise RuntimeError(f"Could not activate view '{name}'.")
    entity = draw(sketch_manager(doc), parent)
    if entity is None:
        raise RuntimeError("SOLIDWORKS did not draw the reference geometry in the view.")
    clear_selection(doc)
    from .sw_core import select_object

    if not select_object(doc, entity, mark=0, append=False):
        raise RuntimeError("Could not select the reference geometry just drawn.")
    return name, entity


@tool(
    "insert_section_view",
    "Create a section view: give the cutting line's two endpoints and where the section should "
    "sit, and the line is drawn in the parent view for you. Give the line either as two points in "
    "millimetres on the sheet (read the parent's position from list_drawing_views first) or as a model "
    "point it passes through plus a direction, which draws it across the whole parent view. Succeeds only "
    "when SOLIDWORKS actually cut: the new view must be a section view with hatched faces. A view that cut "
    "nothing is deleted again and the call fails; when SOLIDWORKS returns no view at all, the cutting line or sketch "
    "line this call made is deleted (line_removed, sketch_line_removed report it).",
    {
        "x1_mm": {"type": "number", "description": "Cutting line start, on the sheet."},
        "y1_mm": {"type": "number"},
        "x2_mm": {"type": "number", "description": "Cutting line end, on the sheet."},
        "y2_mm": {"type": "number"},
        "through_model_mm": {"type": "array", "items": {"type": "number"},
                             "description": "Model point [x, y, z] in millimetres the cutting line passes through, "
                                            "instead of x1..y2."},
        "direction": {"type": "string", "enum": ["vertical", "horizontal"], "default": "vertical",
                      "description": "Direction of the cutting line on the sheet, with through_model_mm."},
        "place_x_mm": {"type": "number", "description": "Where the section view goes."},
        "place_y_mm": {"type": "number"},
        "parent_view": {"type": "string", "description": "View to cut. Defaults to the last one placed."},
        "label": {"type": "string", "default": "A"},
        "depth_mm": {"type": "number", "default": 0, "description": "Partial-section depth; 0 cuts all the way."},
        "reverse": {"type": "boolean", "default": False, "description": "Look the other way along the cutting line."},
        "exclude_fasteners": {"type": "boolean", "default": False},
        "use_selection": {"type": "boolean", "default": False, "description": "Use the already-selected line instead of drawing one."},
    },
    ["place_x_mm", "place_y_mm"],
)
def insert_section_view(args: dict[str, Any]) -> dict[str, Any]:
    from .sw_core import empty_variant

    _, doc = require_drawing()
    parent = args.get("parent_view")
    line_mm: list[float] | None = None
    drawn: Any = None
    label = str(args.get("label", "A"))
    if label in _section_labels(doc):
        # two lines with one label would make the clean-up and delete_drawing_view ambiguous
        return result(False, f"Label '{label}' is already used by a cutting line on this sheet; pass another label.",
                      labels_in_use=sorted(_section_labels(doc)))
    if not bool(args.get("use_selection", False)):
        through = args.get("through_model_mm")
        if through is not None:
            if not isinstance(through, (list, tuple)) or len(through) != 3:
                return result(False, "through_model_mm takes three numbers [x, y, z].")
            view = _find_view(doc, parent)
            if view is None:
                return result(False, "No parent drawing view. Place a view before sectioning it.")
            outline = [to_mm(c) for c in list(safe(view, "GetOutline") or [])[:4]]
            if len(outline) != 4:
                return result(False, "SOLIDWORKS did not report the parent view's outline.")
            px, py = _sheet_point(view, list(through))
            margin = 5.0
            if str(args.get("direction", "vertical")) == "vertical":
                line_mm = [px, outline[1] - margin, px, outline[3] + margin]
            else:
                line_mm = [outline[0] - margin, py, outline[2] + margin, py]
        else:
            for key in ("x1_mm", "y1_mm", "x2_mm", "y2_mm"):
                if args.get(key) is None:
                    return result(False, "Pass the cutting line endpoints or through_model_mm, or set "
                                         f"use_selection. Missing {key}.")
            line_mm = [float(args[k]) for k in ("x1_mm", "y1_mm", "x2_mm", "y2_mm")]

        def draw(sm: Any, view: Any) -> Any:
            a = _sketch_point(view, line_mm[0], line_mm[1])
            b = _sketch_point(view, line_mm[2], line_mm[3])
            return sm.CreateLine(a[0], a[1], 0.0, b[0], b[1], 0.0)

        parent, drawn = _draw_in_view(doc, parent, draw)

    # every view's cutting lines before the call: the view that gains one is the parent, also with use_selection and
    # no parent_view named
    lines_before = _section_line_counts(doc)
    # swCreateSectionViewAtOptions_e, read from the type library: 1 = NotAligned, 2 = OffsetSection,
    # 4 = ChangeDirection, 8 = ScaleWithModel, 16 = Partial, 32 = DisplaySurfaceCut, 64 = ExcludeFasteners.
    # 0 is the aligned full section.
    options = (4 if bool(args.get("reverse", False)) else 0) | (64 if bool(args.get("exclude_fasteners", False)) else 0)
    view = doc.CreateSectionViewAt5(
        to_m(args["place_x_mm"]), to_m(args["place_y_mm"]), 0.0,
        label, options, empty_variant(),
        to_m(args.get("depth_mm", 0)),
    )
    extra: dict[str, Any] = {"line_mm": [round(c, 4) for c in line_mm]} if line_mm else {}
    if view is None:
        # no view, but SOLIDWORKS may still have turned the drawn line into a cutting line (which would also hold the
        # label), or left the sketch line in the parent: remove what this call created
        owner, added = _new_section_lines(doc, lines_before)
        line_removed = all([_delete_section_line(doc, line) for line in added]) if added else None
        sketch_removed = _delete_entity(doc, drawn) if drawn is not None and not added else None
        return result(
            False,
            "SOLIDWORKS did not create the section view. Check that the cutting line actually "
            "crosses the parent view's geometry."
            + (" Its cutting line could not be deleted and is still in the parent view." if line_removed is False
               else " The drawn line could not be deleted and is still in the parent view." if sketch_removed is False
               else ""),
            parent=owner or parent, line_removed=line_removed, sketch_line_removed=sketch_removed, **extra,
        )
    rebuild(doc)
    entry = _view_entry(view, -1)
    owner, added = _new_section_lines(doc, lines_before)
    parent = owner or parent
    parent_view = _find_view(doc, parent) if parent else None
    if parent_view is not None:
        extra["parent_section_lines"] = len(as_list(safe(parent_view, "GetSectionLines")))
    if entry.get("type") != "section" or not entry.get("face_hatches"):
        removed = _delete_view(doc, entry["name"]) if entry.get("name") else False
        # the section line stays in the parent after its view is deleted: delete the lines this call added
        line_removed = None if not added else all([_delete_section_line(doc, line) for line in added])
        if parent_view is not None:
            extra["parent_section_lines"] = len(as_list(safe(parent_view, "GetSectionLines")))
        return result(
            False,
            f"SOLIDWORKS placed '{entry.get('name')}' but it cut nothing (type {entry.get('type')}, "
            f"{entry.get('face_hatches', 0)} hatched faces); "
            + ("the view was deleted again" if removed else "the view could not be deleted and is still on the sheet")
            + (", and so was its cutting line. " if line_removed
               else ", but its cutting line is still in the parent view. " if line_removed is False
               else "; no new cutting line was found. ")
            + "Check that the cutting line crosses the part.",
            parent=parent, view=entry, removed=removed, line_removed=line_removed, **extra,
        )
    return result(
        True, f"Created section view {args.get('label', 'A')} with {entry['face_hatches']} hatched faces.",
        parent=parent, view=entry, **extra,
    )


@tool(
    "insert_detail_view",
    "Create a detail view: give the detail circle's centre and radius and where the enlarged view "
    "should sit, and the circle is drawn in the parent view for you. Coordinates are millimetres "
    "on the sheet, so read the parent's position from list_drawing_views first. The scale is "
    "absolute (model to paper), NOT relative to the parent: on a 1:4 sheet, asking for 2:1 makes "
    "the detail eight times the parent view and it will overflow the sheet. Pick a scale near the "
    "sheet scale — 1:2 on a 1:4 sheet gives a sensible 2x enlargement.",
    {
        "center_x_mm": {"type": "number", "description": "Detail circle centre, on the sheet."},
        "center_y_mm": {"type": "number"},
        "radius_mm": {"type": "number", "description": "Detail circle radius, on the sheet."},
        "place_x_mm": {"type": "number", "description": "Where the enlarged view goes."},
        "place_y_mm": {"type": "number"},
        "parent_view": {"type": "string", "description": "View to detail. Defaults to the last one placed."},
        "label": {"type": "string", "default": "I"},
        "scale_numerator": {"type": "number", "default": 2},
        "scale_denominator": {"type": "number", "default": 1},
        "full_outline": {"type": "boolean", "default": False},
        "jagged_outline": {"type": "boolean", "default": False},
        "use_selection": {"type": "boolean", "default": False, "description": "Use the already-selected circle instead of drawing one."},
    },
    ["place_x_mm", "place_y_mm"],
)
def insert_detail_view(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    parent = args.get("parent_view")
    if not bool(args.get("use_selection", False)):
        for key in ("center_x_mm", "center_y_mm", "radius_mm"):
            if args.get(key) is None:
                return result(False, f"Pass the detail circle, or set use_selection. Missing {key}.")
        def draw(sm: Any, view: Any) -> Any:
            cx, cy = _sketch_point(view, float(args["center_x_mm"]), float(args["center_y_mm"]))
            return sm.CreateCircleByRadius(cx, cy, 0.0, to_m(args["radius_mm"]) / _view_scale(view))

        parent, _ = _draw_in_view(doc, parent, draw)

    detail_args = (
        to_m(args["place_x_mm"]), to_m(args["place_y_mm"]), 0.0,
        1,  # swDetailCircleStyle_e: 1 = circle profile
        float(args.get("scale_numerator", 2)), float(args.get("scale_denominator", 1)),
        str(args.get("label", "I")),
        2,  # swDetViewStyle_e: 2 = label with a leader
        bool(args.get("full_outline", False)),
        bool(args.get("jagged_outline", False)),
        False, 0,
    )
    # CreateDetailViewAt3 (2016) stops after FullOutline: no jagged outline,
    # no fixed-size circle, no border style.
    view = call_versioned(
        doc,
        ("CreateDetailViewAt4", detail_args),
        ("CreateDetailViewAt3", detail_args[:9]),
    )
    if view is None:
        return result(
            False,
            "SOLIDWORKS did not create the detail view. Check that the detail circle lies over the "
            "parent view's geometry.",
            parent=parent,
        )
    rebuild(doc)
    return result(
        True, f"Created detail view {args.get('label', 'I')}.",
        parent=parent, view=_view_entry(view, -1),
    )


@tool(
    "list_drawing_views",
    "Read-only: list the views on the active sheet of the drawing with their names, types, positions, scales, "
    "outlines, hatched faces, section lines drawn in them, how many dimensions each carries, and the "
    "transform from model millimetres to sheet millimetres (sheet = p·R·scale + translation, R row-major). "
    "The first entry is the sheet itself.",
    {},
)
def list_drawing_views(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    views = []
    for i, v in enumerate(_iter_views(doc)):
        entry = _view_entry(v, i)
        if not _is_sheet(v):
            transform = _model_to_sheet(v)
            if transform is not None:
                entry["model_to_sheet"] = transform
        views.append(entry)
    return result(True, f"Read {len(views)} drawing views.", views=views)


def _sheet_size_mm(sheet: Any) -> tuple[float, float] | None:
    """Width and height of an ISheet from GetProperties2 ([size, template, scale1, scale2, first angle, w, h])."""
    properties = safe(sheet, "GetProperties2")
    if properties is None or len(properties) < 7:
        return None
    return to_mm(properties[5]), to_mm(properties[6])


def _current_sheet_name(doc: Any) -> str:
    return str(safe(value(doc, "GetCurrentSheet"), "GetName", "") or "")


DIMENSION_WALK_LIMIT = 20000


def _display_dimensions(view: Any) -> list[Any]:
    """The display dimensions SOLIDWORKS walks for a view. On 2016 SP3 hidden ones are no longer part of the walk; the
    API documentation says suppressed ones are, so _all_dimensions filters them. A walk that does not end within
    DIMENSION_WALK_LIMIT steps is an error, never a silently shortened list."""
    found, display = [], safe(view, "GetFirstDisplayDimension5")
    while display is not None:
        if len(found) >= DIMENSION_WALK_LIMIT:
            raise RuntimeError(f"The dimension walk of view '{safe(view, 'GetName2', '')}' did not end after "
                               f"{DIMENSION_WALK_LIMIT} dimensions.")
        found.append(display)
        display = safe(display, "GetNext5")
    return found


def _dimension_entry(view_name: str, display: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"view": view_name}
    try:
        dimension = flag_methods(display, "GetDimension2").GetDimension2(0)
        entry["full_name"] = str(safe(dimension, "FullName", "") or "")
        system_value = float(safe(dimension, "SystemValue", 0.0) or 0.0)
        if int(safe(dimension, "GetType", 0) or 0) == 3:   # swDimensionType_e: angular
            entry["value_deg"] = round(system_value * 180.0 / 3.141592653589793, 6)
        else:
            entry["value_mm"] = round(to_mm(system_value), 6)
    except Exception:
        logger.info("Could not read the model dimension behind a display dimension in %s", view_name)
    kind = safe(display, "Type2")
    entry["type2"] = int(kind) if kind is not None else None
    parentheses = safe(display, "ShowParenthesis")
    entry["parentheses"] = bool(parentheses) if parentheses is not None else None
    annotation = safe(display, "GetAnnotation")
    position = safe(annotation, "GetPosition") if annotation is not None else None
    if position is not None and len(position) >= 2:
        entry["text_mm"] = [round(to_mm(position[0]), 3), round(to_mm(position[1]), 3)]
    visible = safe(annotation, "Visible") if annotation is not None else None
    entry["visible"] = None if visible is None else int(visible) == 1   # swAnnotationVisibilityState_e
    layer = safe(annotation, "Layer") if annotation is not None else None
    entry["layer"] = str(layer) if layer else None
    entry["selection_name"] = str(safe(display, "GetNameForSelection", "") or "")
    return entry


def _late_bound(obj: Any) -> Any:
    """IDrawingDoc.GetViews hands back bare PyIDispatch pointers (measured on 2016 SP3): no member reads until wrapped."""
    if type(obj).__name__ == "PyIDispatch":
        import win32com.client

        return win32com.client.Dispatch(obj)
    return obj


def _views_of_every_sheet(doc: Any) -> list[tuple[str, Any]]:
    """(sheet name, model view) for every sheet: IDrawingDoc.GetViews gives one array per sheet, the sheet first. A
    drawing always has a sheet, so an empty answer is an error, not an empty drawing."""
    sheets = as_list(safe(doc, "GetViews"))
    if not sheets:
        raise RuntimeError("SOLIDWORKS did not list the views of the drawing's sheets (GetViews).")
    found: list[tuple[str, Any]] = []
    for sheet_views in sheets:
        views = [_late_bound(v) for v in as_list(sheet_views)]
        sheet_name = str(safe(views[0], "GetName2", "") or "") if views else ""
        found.extend((sheet_name, v) for v in views[1:] if not _is_sheet(v))
    return found


def _all_dimensions(doc: Any, view_names: set[str] | None = None) -> list[dict[str, Any]]:
    """Every visible dimension of every sheet, or of the named views, each with its sheet and that sheet's size. A
    display the walk returns although it is hidden (documented for suppressed dimensions) is left out, and so is one
    on a hidden drawing layer: IAnnotation.Visible does not see the layer."""
    sizes: dict[str, tuple[float, float] | None] = {}
    layers: dict[str, bool] = {}

    def layer_shown(name: str) -> bool:
        if name not in layers:
            try:
                found = flag_methods(safe(doc, "GetLayerManager"), "GetLayer").GetLayer(name)
                shown = safe(found, "Visible")
                layers[name] = shown is None or bool(shown)
            except Exception:
                layers[name] = True   # an unreadable layer hides nothing it could be shown to hide
        return layers[name]

    entries: list[dict[str, Any]] = []
    for sheet_name, view in _views_of_every_sheet(doc):
        name = str(safe(view, "GetName2", "") or "")
        if view_names is not None and name not in view_names:
            continue
        if sheet_name not in sizes:
            try:
                sizes[sheet_name] = _sheet_size_mm(flag_methods(doc, "Sheet").Sheet(sheet_name))
            except Exception:
                sizes[sheet_name] = None
        for display in _display_dimensions(view):
            entry = _dimension_entry(name, display)
            if entry.get("visible") is False or (entry.get("layer") and not layer_shown(entry["layer"])):
                continue
            entry["sheet"] = sheet_name
            size = sizes[sheet_name]
            text = entry.get("text_mm")
            entry["outside_sheet"] = None if size is None or not text else not (
                0.0 <= text[0] <= size[0] and 0.0 <= text[1] <= size[1])
            entries.append(entry)
    return entries


def _view_sheet(doc: Any, view_name: str) -> str | None:
    return next((sheet for sheet, v in _views_of_every_sheet(doc) if str(safe(v, "GetName2", "")) == view_name), None)


def _duplicates(entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Model dimensions visible in more than one view of the same sheet; a dimension repeated on another sheet is a
    deliberate repeat, not a duplicate. Keyed `full_name` on a single sheet, `full_name @ sheet` otherwise."""
    views: dict[tuple[str, str], list[str]] = {}
    for entry in entries:
        if entry.get("full_name") and entry.get("visible") is not False:
            views.setdefault((entry["full_name"], entry.get("sheet", "")), []).append(entry["view"])
    one_sheet = len({sheet for _, sheet in views}) <= 1
    return {(name if one_sheet else f"{name} @ {sheet}"): found for (name, sheet), found in views.items()
            if len(found) > 1}


def _outside_sheet(entries: list[dict[str, Any]]) -> int:
    return sum(1 for e in entries if e.get("outside_sheet"))


def _on_sheet(doc: Any, sheet: str, act: Any) -> Any:
    """Run `act` with `sheet` active and the previous sheet active again afterwards; the selection is cleared on the
    way out, also after an exception."""
    previous = _current_sheet_name(doc)
    switched = bool(sheet) and sheet != previous
    try:
        if switched and not bool(doc.ActivateSheet(sheet)):
            raise RuntimeError(f"Could not activate sheet '{sheet}'.")
        return act()
    finally:
        clear_selection(doc)
        if switched:
            doc.ActivateSheet(previous)


@tool(
    "list_drawing_dimensions",
    "Read-only: every visible dimension on every sheet of the active drawing, or on one view, with its sheet, the "
    "view it sits in, the model dimension behind it (full_name, as set_dimension takes it), its value, "
    "swDimensionType_e of the display (type2), where its text sits on its sheet and whether that is outside the sheet, "
    "whether it shows in parentheses, and the name to select it by. `duplicates` lists every model dimension visible in "
    "more than one view of the same sheet; insert_model_annotations produces those even with duplicate_dimensions "
    "false. Hidden dimensions, also those on a hidden drawing layer, are not listed; each entry names its layer.",
    {"view_name": {"type": "string", "description": "Only this view, on whichever sheet it is."}},
)
def list_drawing_dimensions(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    view_name = args.get("view_name")
    if view_name and _view_sheet(doc, str(view_name)) is None:
        return result(False, f"No view named '{view_name}' on any sheet.")
    entries = _all_dimensions(doc, {str(view_name)} if view_name else None)
    return result(True, f"Read {len(entries)} dimensions.", dimensions=entries, duplicates=_duplicates(entries),
                  outside_sheet=_outside_sheet(entries))


@tool(
    "hide_drawing_dimensions",
    "Hide or delete dimensions on the active drawing: name each by its view and the full_name list_drawing_dimensions "
    "reports. Per sheet, every display of that model dimension in that view is selected by name, then one "
    "HideDimension (mode hide) or DeleteSelection2 (mode delete) acts on all of them; a view on another sheet has its "
    "sheet activated for that and the previous sheet activated again. The part is not changed either way. Succeeds "
    "only when none of them is still listed afterwards; a dimension that is already hidden is not listed and counts as "
    "not on the drawing.",
    {
        "dimensions": {
            "type": "array",
            "items": {"type": "object", "properties": {"view": {"type": "string"}, "full_name": {"type": "string"}},
                      "required": ["view", "full_name"]},
        },
        "mode": {"type": "string", "enum": ["hide", "delete"], "default": "hide"},
    },
    ["dimensions"],
)
def hide_drawing_dimensions(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    wanted = [(str(d.get("view", "")), str(d.get("full_name", ""))) for d in (args.get("dimensions") or [])
              if isinstance(d, dict)]
    if not wanted:
        return result(False, "Name at least one dimension as {view, full_name}.")
    mode = str(args.get("mode", "hide"))
    if mode not in ("hide", "delete"):
        return result(False, f"Unknown mode '{mode}'; use hide or delete.")
    targets = set(wanted)
    views = {v for v, _ in targets}
    entries = _all_dimensions(doc, views)
    found = [e for e in entries if (e["view"], e.get("full_name", "")) in targets]
    missing = sorted({f"{v}: {n}" for v, n in targets} - {f"{e['view']}: {e.get('full_name', '')}" for e in found})
    selected, unselected = 0, []
    ext = extension(doc)
    for sheet in sorted({e["sheet"] for e in found}):
        on_sheet = [e for e in found if e["sheet"] == sheet]

        def act(on_sheet: list[dict[str, Any]] = on_sheet) -> int:
            clear_selection(doc)
            count = 0
            for entry in on_sheet:
                name = entry.get("selection_name")
                if name and bool(ext.SelectByID2(name, "DIMENSION", 0.0, 0.0, 0.0, True, 0, nothing(), 0)):
                    count += 1
                else:
                    unselected.append(f"{entry['view']}: {entry.get('full_name')}")
            if count:
                if mode == "hide":
                    flag_methods(doc, "HideDimension").HideDimension()
                else:
                    flag_methods(ext, "DeleteSelection2").DeleteSelection2(0)
            return count

        selected += _on_sheet(doc, sheet, act)
    rebuild(doc)
    after = _all_dimensions(doc, views)
    remaining = sorted({f"{e['view']}: {e.get('full_name')}" for e in after
                        if (e["view"], e.get("full_name", "")) in targets})
    ok = selected > 0 and not remaining and not missing and not unselected
    if ok:
        message = f"{'Hid' if mode == 'hide' else 'Deleted'} {selected} dimensions."
    else:
        parts = []
        if missing:
            parts.append(f"not on the drawing: {', '.join(missing)}")
        if unselected:
            parts.append(f"could not be selected: {', '.join(unselected)}")
        if remaining:
            parts.append(f"still visible: {', '.join(remaining)}")
        message = "Not every dimension went away; " + "; ".join(parts or ["nothing was selected"]) + "."
    everything = _all_dimensions(doc)
    return result(ok, message, mode=mode, selected=selected, missing=missing, unselected=unselected,
                  remaining=remaining, dimensions_left=len(everything), duplicates=_duplicates(everything))


@tool(
    "arrange_drawing_dimensions",
    "Auto-arrange the visible dimensions of one drawing view (AlignDimensions with AutoArrange): SOLIDWORKS "
    "spaces them out from the view at spacing_mm; a view on another sheet has its sheet activated for that and the "
    "previous sheet activated again. Fails, arranging nothing, when not every visible dimension of the view can be "
    "selected. Reports how many dimension texts lie outside the view's sheet before and after.",
    {
        "view_name": {"type": "string"},
        "spacing_mm": {"type": "number", "default": 6, "description": "Space between dimension lines."},
    },
    ["view_name"],
)
def arrange_drawing_dimensions(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    view_name = str(args["view_name"])
    sheet = _view_sheet(doc, view_name)
    if sheet is None:
        return result(False, f"No view named '{view_name}' on any sheet.")
    before = _all_dimensions(doc, {view_name})
    if not before:
        return result(True, f"View '{view_name}' carries no visible dimension; nothing to arrange.", arranged=0,
                      selected=0, outside_before=0, outside_after=0)
    ext = extension(doc)

    def act() -> tuple[int, bool]:
        clear_selection(doc)
        count = sum(1 for e in before if e.get("selection_name") and bool(
            ext.SelectByID2(e["selection_name"], "DIMENSION", 0.0, 0.0, 0.0, True, 0, nothing(), 0)))
        if count != len(before):
            return count, False
        # swAlignDimensionType_AutoArrange = 0
        return count, bool(flag_methods(ext, "AlignDimensions").AlignDimensions(0, to_m(float(args.get("spacing_mm", 6)))))

    selected, aligned = _on_sheet(doc, sheet, act)
    rebuild(doc)
    after = _all_dimensions(doc, {view_name})
    return result(
        aligned,
        f"Arranged {selected} dimensions in '{view_name}'." if aligned
        else f"Did not arrange the dimensions of '{view_name}': {selected} of {len(before)} could be selected"
             + ("." if selected < len(before) else ", and AlignDimensions returned false."),
        arranged=selected if aligned else 0, selected=selected, outside_before=_outside_sheet(before),
        outside_after=_outside_sheet(after),
    )


@tool(
    "delete_drawing_view",
    "Delete a drawing view on the active sheet. For a section view, also delete its cutting line from the parent view, which "
    "SOLIDWORKS otherwise leaves behind. Succeeds only when the view is gone afterwards.",
    {"name": {"type": "string", "description": "View name from list_drawing_views."}},
    ["name"],
)
def delete_drawing_view(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    name = str(args["name"])
    view = _find_view(doc, name)
    if view is None:
        return result(False, f"No view named '{name}'. Call list_drawing_views for the names.")
    # the cutting line that belongs to this section view: the one whose section view it is
    line = None
    if int(safe(view, "Type", 0) or 0) == 2:
        line = next((ln for v in _iter_views(doc) if not _is_sheet(v) for ln in as_list(safe(v, "GetSectionLines"))
                     if str(safe(safe(ln, "GetSectionView"), "GetName2", "") or "") == name), None)
    line_name = str(safe(line, "GetName", "") or "") if line is not None else ""
    removed = _delete_view(doc, name)
    line_removed = None
    if line_name:
        line_removed = _delete_selected(doc, line_name, "SECTIONLINE") or not any(
            str(safe(ln, "GetName", "") or "") == line_name for v in _iter_views(doc) if not _is_sheet(v)
            for ln in as_list(safe(v, "GetSectionLines")))
    rebuild(doc)
    gone = _find_view(doc, name) is None
    ok = removed and gone and line_removed is not False
    return result(ok, f"Deleted view '{name}'" + (" and its cutting line." if line_removed else ".") if ok
                  else f"View '{name}' was not deleted completely (view gone: {gone}, cutting line removed: "
                       f"{line_removed}).", removed=gone, line_removed=line_removed)


@tool(
    "activate_drawing_view",
    "Make a drawing view the active one, which is what section, detail, projected views and "
    "sketching inside a view all operate on.",
    {"name": {"type": "string", "description": "View name from list_drawing_views."}},
    ["name"],
)
def activate_drawing_view(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    name = str(args["name"])
    ok = bool(doc.ActivateView(name))
    return result(ok, f"Activated view '{name}'." if ok else f"No view named '{name}'.")


@tool(
    "create_drawing_sketch",
    "Open a 2D sketch in a drawing view. Use draw_line or draw_circle next to define a section or "
    "detail boundary; the sketch remains open and its geometry selected for insert_section_view or "
    "insert_detail_view.",
    {"view_name": {"type": "string", "description": "Parent view to sketch in. Defaults to the active drawing view."}},
)
def create_drawing_sketch(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    view = _activate(doc, args.get("view_name"))
    name = str(safe(view, "GetName2", "") or "")
    if not name or not bool(doc.ActivateView(name)):
        return result(False, "SOLIDWORKS could not activate a drawing view for sketching.")
    clear_selection(doc)
    sketch_manager(doc).InsertSketch(True)
    if doc.SketchManager.ActiveSketch is None:
        return result(False, f"SOLIDWORKS did not open a sketch in drawing view '{name}'.")
    return result(True, f"Opened a sketch in drawing view '{name}'.", view_name=name)


@tool(
    "set_drawing_view",
    "Move, rescale, or restyle an existing drawing view. Position is millimetres from the sheet's "
    "lower-left corner.",
    {
        "name": {"type": "string"},
        "x_mm": {"type": "number"},
        "y_mm": {"type": "number"},
        "scale": {"type": "number", "description": "Decimal scale, e.g. 0.25 for 1:4."},
        "display_mode": DISPLAY_MODE_SCHEMA,
        "tangent_edges": {
            "type": "string",
            "enum": ["visible", "hidden", "phantom"],
            "description": "How tangent edges are drawn.",
        },
    },
    ["name"],
)
def set_drawing_view(args: dict[str, Any]) -> dict[str, Any]:
    from .sw_core import double_array

    _, doc = require_drawing()
    name = str(args["name"])
    target = next((v for v in _iter_views(doc) if str(safe(v, "GetName2", "")) == name), None)
    if target is None:
        return result(False, f"No view named '{name}'. Call list_drawing_views for the names.")

    if args.get("x_mm") is not None and args.get("y_mm") is not None:
        target.Position = double_array([to_m(args["x_mm"]), to_m(args["y_mm"])])
    display = _apply_view_options(target, args)
    if args.get("tangent_edges"):
        mode = {"visible": 1, "hidden": 2, "phantom": 3}[str(args["tangent_edges"])]
        try:
            target.TangentEdgeDisplay = mode
        except Exception:
            logger.info("Could not set tangent edge display.")
    rebuild(doc)
    if display is not None and not display["ok"]:
        return result(False, _display_failure([display]), view=_view_entry(target, -1), display=[display])
    return result(True, f"Updated view '{name}'.", view=_view_entry(target, -1))


# --------------------------------------------------------------------------
# Annotation
# --------------------------------------------------------------------------


@tool(
    "insert_model_annotations",
    "Import the model's own dimensions and annotations onto the drawing (Insert > Model Items). "
    "This is the right tool when the part was modelled with the dimensions you want shown. "
    "For dimensions invented from the view geometry instead, use auto_dimension_view.",
    {
        "types": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(ANNOTATION_TYPES)},
            "default": ["marked_for_drawing", "not_marked_for_drawing"],
            "description": "Which annotation kinds to import.",
        },
        "view_name": {
            "type": "string",
            "description": "Target drawing view when all_views is false. Defaults to the first model view.",
        },
        "all_views": {"type": "boolean", "default": True, "description": "Import into every view, not just the active one."},
        "duplicate_dimensions": {"type": "boolean", "default": False, "description": "Allow the same dimension in more than one view."},
        "hidden_feature_dimensions": {"type": "boolean", "default": False},
        "source": {
            "type": "string",
            "enum": ["entire_model", "selected_feature", "selected_component", "assembly_only"],
            "default": "entire_model",
        },
    },
)
def insert_model_annotations(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    wanted = [str(t) for t in (args.get("types") or ["marked_for_drawing", "not_marked_for_drawing"])]
    mask = 0
    for name in wanted:
        mask |= ANNOTATION_TYPES[name]
    source = {
        "entire_model": 0, "selected_feature": 1,
        "selected_component": 2, "assembly_only": 3,
    }[str(args.get("source", "entire_model"))]

    before = sum(int(v.get("dimensions", 0)) for v in
                 (_view_entry(x, i) for i, x in enumerate(_iter_views(doc))))

    # InsertModelAnnotations3 works on the *selected* drawing view, not merely
    # the active one.  Activating a view alone was why a part containing seven
    # dimensions consistently imported zero annotations.  Select a concrete
    # model view by its localized name before invoking the API.
    view = _activate(doc, args.get("view_name"))
    view_name = str(safe(view, "GetName2", "") or "")
    if not view_name:
        return result(False, "Could not resolve a model drawing view for model-item import.")
    clear_selection(doc)
    selected = bool(
        extension(doc).SelectByID2(view_name, "DRAWINGVIEW", 0.0, 0.0, 0.0, False, 0, nothing(), 0)
    )
    if not selected:
        return result(False, f"SOLIDWORKS could not select drawing view '{view_name}'.")
    if not bool(doc.ActivateView(view_name)):
        return result(False, f"SOLIDWORKS could not activate drawing view '{view_name}'.")

    inserted = doc.InsertModelAnnotations3(
        source, mask,
        bool(args.get("all_views", True)),
        bool(args.get("duplicate_dimensions", False)),
        bool(args.get("hidden_feature_dimensions", False)),
        False,
    )
    rebuild(doc)
    clear_selection(doc)
    views = [_view_entry(v, i) for i, v in enumerate(_iter_views(doc))]
    after = sum(int(v.get("dimensions", 0)) for v in views)
    added = after - before
    # The return is an array of the annotations placed, but it comes back empty
    # in plenty of valid cases, so report the dimension-count delta instead.
    return result(
        added > 0 or bool(as_list(inserted)),
        f"Imported {added} dimensions." if added > 0
        else "No model items were imported. The model may have no dimensions marked for drawing; "
             "try types including not_marked_for_drawing, or use auto_dimension_view.",
        types=wanted,
        view_name=view_name,
        dimensions_added=added,
        views=views,
    )


@tool(
    "auto_dimension_view",
    "Run DimXpert over a drawing view and generate dimensions from its geometry. Use this when the "
    "model has no dimensions worth importing; to reuse the model's own dimensions call "
    "insert_model_annotations instead. The view must be active first.",
    {
        "view_name": {"type": "string", "description": "View to dimension. Defaults to the active view."},
        "entities": {"type": "string", "enum": sorted(AUTODIM_ENTITIES), "default": "all"},
        "horizontal_scheme": {"type": "string", "enum": sorted(AUTODIM_SCHEMES), "default": "baseline"},
        "horizontal_placement": {"type": "string", "enum": sorted(HORIZONTAL_PLACEMENT), "default": "below"},
        "vertical_scheme": {"type": "string", "enum": sorted(AUTODIM_SCHEMES), "default": "baseline"},
        "vertical_placement": {"type": "string", "enum": sorted(VERTICAL_PLACEMENT), "default": "left"},
    },
)
def auto_dimension_view(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    if args.get("view_name"):
        if not bool(doc.ActivateView(str(args["view_name"]))):
            return result(False, f"No view named '{args['view_name']}'.")

    before = [_view_entry(v, i) for i, v in enumerate(_iter_views(doc))]
    status = int(
        doc.AutoDimension(
            AUTODIM_ENTITIES[str(args.get("entities", "all"))],
            AUTODIM_SCHEMES[str(args.get("horizontal_scheme", "baseline"))],
            HORIZONTAL_PLACEMENT[str(args.get("horizontal_placement", "below"))],
            AUTODIM_SCHEMES[str(args.get("vertical_scheme", "baseline"))],
            VERTICAL_PLACEMENT[str(args.get("vertical_placement", "left"))],
        )
    )
    rebuild(doc)
    after = [_view_entry(v, i) for i, v in enumerate(_iter_views(doc))]
    added = sum(int(v.get("dimensions", 0)) for v in after) - sum(int(v.get("dimensions", 0)) for v in before)
    return result(
        added > 0,
        f"Auto-dimensioning added {added} dimensions."
        if added > 0 else f"Auto-dimensioning added nothing (status {status}).",
        status=status,
        dimensions_added=added,
        views=after,
    )


def _activate(doc: Any, view_name: str | None) -> Any:
    """Activate the named view (or use the active one) and return the view object."""
    if view_name and not bool(doc.ActivateView(str(view_name))):
        raise RuntimeError(f"No view named '{view_name}'.")
    # The API requires the object returned by ActiveDrawingView after activation
    # for operations such as AutoInsertCenterMarks2.  A view object retained
    # while walking the sheet is not always accepted as that active view.
    active = safe(doc, "ActiveDrawingView")
    if active is not None:
        return flag_methods(active, "GetVisibleEntities2", "GetVisibleEntities", "SelectEntity", "AutoInsertCenterMarks2")

    target = None
    for view in _iter_views(doc):
        if view_name:
            if str(safe(view, "GetName2", "")) == str(view_name):
                target = view
                break
        elif safe(view, "Type") is not None and int(safe(view, "Type", 1) or 1) != 1:
            target = view
    if target is None:
        raise RuntimeError("No drawing view is available. Place a view first.")
    return target


def _select_circular_edges(doc: Any, view: Any) -> int:
    """Select every circular edge visible in the view.

    InsertCenterMark3 annotates the current selection rather than the whole
    view, so a tool that means "mark every hole here" has to build that
    selection itself.
    """
    from .sw_core import nothing

    clear_selection(doc)
    count = 0
    entities: list[Any] = []
    flag_methods(view, "GetVisibleEntities2", "GetVisibleEntities", "SelectEntity")
    # swViewEntityType_Edge = 1. The component argument must be a typed null for
    # a part view, which a bare Python None does not marshal to.
    for member in ("GetVisibleEntities2", "GetVisibleEntities"):
        for component in (nothing(), None):
            try:
                entities = as_list(getattr(view, member)(component, 1))
            except Exception:
                continue
            if entities:
                break
        if entities:
            break
    if not entities:
        logger.info("Could not enumerate the view's visible edges.")
        return 0
    for entity in entities:
        try:
            curve = value(entity, "GetCurve")
            if curve is None or int(value(curve, "Identity")) != 3002:  # circle
                continue
        except Exception:
            continue
        # IView::SelectEntity is the drawing-aware selection path.  Calling
        # Edge::Select4 here selects the corresponding model edge (or silently
        # selects nothing), so InsertCenterMark3/InsertCenterLine2 see no
        # drawing geometry even though GetVisibleEntities2 found it.
        if view.SelectEntity(entity, True):
            count += 1
    return count


def _center_mark_count(view: Any) -> int | None:
    """Read legacy center-mark features when the drawing exposes them.

    Modern SOLIDWORKS creates annotation center marks, for which this count is
    often zero.  It is therefore informational only; a successful insertion
    result remains authoritative.
    """
    try:
        size = byref_long(0)
        flag_methods(view, "GetCenterMarkCount2")
        return int(view.GetCenterMarkCount2(size))
    except Exception:
        return None


@tool(
    "insert_center_marks",
    "Add center marks to the circular edges of a drawing view. By default every circle in the view "
    "gets one; pass selected_only if you have already made a selection.",
    {
        "view_name": {"type": "string", "description": "Defaults to the active view."},
        "style": {"type": "string", "enum": sorted(CENTER_MARK_STYLES), "default": "single"},
        "propagate": {"type": "boolean", "default": True},
        "slots": {"type": "boolean", "default": False},
        "selected_only": {"type": "boolean", "default": False, "description": "Annotate the existing selection instead of every circle."},
    },
)
def insert_center_marks(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    view = _activate(doc, args.get("view_name"))
    if not bool(args.get("selected_only", False)):
        # GetVisibleEntities2 does not enumerate part-view edges reliably in
        # late-bound Python.  The API's own view-scoped automatic command does,
        # and covers holes, fillets, and slots (the three supported classes).
        before = _center_mark_count(view)
        ok = bool(
            flag_methods(view, "AutoInsertCenterMarks2").AutoInsertCenterMarks2(
                1 | 2 | 4,  # holes, fillets, slots
                1 | 2 | 8,  # linear, circular, and base connection lines
                bool(args.get("slots", False)), bool(args.get("slots", False)),
                True, 0.0, 0.0, False, False, 0.0,
            )
        )
        rebuild(doc)
        total = _center_mark_count(view)
        return result(
            ok,
            "Inserted automatic center marks." if ok else "SOLIDWORKS added no center marks.",
            method="AutoInsertCenterMarks2",
            legacy_center_mark_features_before=before,
            legacy_center_mark_features=total,
        )

    picked = _select_circular_edges(doc, view)
    marks = doc.InsertCenterMark3(
        CENTER_MARK_STYLES[str(args.get("style", "single"))],
        bool(args.get("propagate", True)),
        bool(args.get("slots", False)),
    )
    rebuild(doc)
    clear_selection(doc)
    return result(
        bool(as_list(marks)),
        "Inserted center marks for the selected entities." if as_list(marks)
        else "SOLIDWORKS added no center marks to the selected entities.",
        circles_selected=picked,
        method="InsertCenterMark3",
    )


@tool(
    "insert_centerlines",
    "Add centerlines to a drawing view. By default every circular edge in the view is used, which "
    "is what gives bolt circles and holes their axes.",
    {
        "view_name": {"type": "string", "description": "Defaults to the active view."},
        "selected_only": {"type": "boolean", "default": False},
    },
)
def insert_centerlines(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    view = _activate(doc, args.get("view_name"))
    if not bool(args.get("selected_only", False)):
        # In a drawing, the axes of circular holes are represented by automatic
        # center *marks* (the visible orthogonal centerlines), not standalone
        # ICenterLine objects.  Asking InsertCenterLine2 to infer them from
        # raw model edges silently does nothing in a part view.  Use the
        # drawing-view command that SOLIDWORKS itself uses for hole axes.
        ok = bool(
            flag_methods(view, "AutoInsertCenterMarks2").AutoInsertCenterMarks2(
                1,  # hole centers
                1 | 2 | 8,
                False, False, True, 0.0, 0.0, False, False, 0.0,
            )
        )
        rebuild(doc)
        return result(
            ok,
            "Inserted hole axes as automatic center marks." if ok
            else "SOLIDWORKS added no hole axes to this view.",
            method="AutoInsertCenterMarks2",
            centerline_kind="center_marks",
        )

    lines = doc.InsertCenterLine2()
    rebuild(doc)
    clear_selection(doc)
    count = len(as_list(lines))
    return result(
        count > 0,
        f"Added {count} centerlines." if count else
        "SOLIDWORKS added no centerlines to the selected entities.",
        count=count,
        method="InsertCenterLine2",
    )


@tool(
    "add_note",
    "Place a text note on the sheet, for things like a 技术要求 block. Position is millimetres from "
    "the sheet's lower-left corner. Use \\n for line breaks.",
    {
        "text": {"type": "string"},
        "x_mm": {"type": "number"},
        "y_mm": {"type": "number"},
        "height_mm": {"type": "number", "description": "Text height; omit to use the document default."},
    },
    ["text", "x_mm", "y_mm"],
)
def add_note(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_drawing()
    clear_selection(doc)
    note = flag_methods(doc, "InsertNote").InsertNote(str(args["text"]))
    if note is None:
        return result(False, "SOLIDWORKS did not create the note.")
    try:
        annotation = value(note, "GetAnnotation")
        annotation.SetPosition(to_m(args["x_mm"]), to_m(args["y_mm"]), 0.0)
        if args.get("height_mm"):
            text_format = value(annotation, "GetTextFormat")
            text_format.CharHeight = to_m(args["height_mm"])
            annotation.SetTextFormat(0, False, text_format)
    except Exception:
        logger.info("The note was created but could not be positioned or restyled.")
    rebuild(doc)
    clear_selection(doc)
    return result(True, "Added a note.", text=str(args["text"]))
