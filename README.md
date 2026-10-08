# SW_MCP

> **Origin.** SW_MCP continues [solidworks-mcp](https://github.com/Slacker-LLC/solidworks-mcp) by JIALE LIU
> (Apache License 2.0), forked at commit 6019ce0 of 2026-08-16 after upstream went quiet. On top of it: fallbacks
> to earlier COM method variants for older SOLIDWORKS releases (verified on 2016 SP3), sketch tooling
> (`fully_define_sketch`, entities without inferencing, origin selections, diameter dimensions, rectangles and
> slots from lines and arcs), `list_open_documents`, `close_document`, silent save and export, and fixes found
> while driving the server from an automated build pipeline. The Python package and the console script keep
> the names `solidworks_mcp` and `solidworks-mcp`, so existing configurations keep working. The compatibility
> part was offered upstream as [PR #2](https://github.com/Slacker-LLC/solidworks-mcp/pull/2).

An MCP server that drives a **running** SOLIDWORKS session over its COM API.

It does not launch SOLIDWORKS, register an add-in, execute arbitrary code, or
touch the network. It attaches to a session you already have open and calls the
documented API — so if a tool can't do something, neither could a macro.

111 tools: sketching with real relations and driving dimensions, the solid
features you actually reach for, sheet metal with its flat pattern and DXF export,
weldments from 3D sketches, reference geometry, assemblies and mates, and —
importantly — a feedback channel, including screenshots returned as images so
the model can see what it just built.

A sibling server, [`autocad-mcp`](https://github.com/limuzi013/autocad-mcp),
does the same for AutoCAD.

<!-- mcp-name: io.github.d-veloping/sw_mcp -->

---

## Requirements

- Windows, with SOLIDWORKS installed and **already running**
- Python 3.10+

## Install

```powershell
git clone https://github.com/d-veloping/SW_MCP.git solidworks-mcp
cd solidworks-mcp
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install .
```

That puts a `solidworks-mcp` command in the environment, which is what the MCP
host runs. `uv` works too, if you prefer it:

```powershell
uv tool install --from git+https://github.com/d-veloping/SW_MCP solidworks-mcp
```

### Claude Code / Claude Desktop

```json
{
  "mcpServers": {
    "solidworks-mcp": {
      "command": "C:\\path\\to\\.venv\\Scripts\\solidworks-mcp.exe"
    }
  }
}
```

### Codex CLI (`~/.codex/config.toml`)

```toml
[mcp_servers.solidworks-mcp]
command = 'C:\path\to\.venv\Scripts\solidworks-mcp.exe'
```

Use single-quoted TOML strings so backslashes survive. Restart the MCP host after
editing its configuration.

An MCP host can also be pointed straight at a checkout, with nothing installed
but the dependencies — `server.py` at the repository root exists for exactly
that:

```toml
[mcp_servers.solidworks-mcp]
command = 'C:\path\to\.venv\Scripts\python.exe'
args = ['C:\path\to\solidworks-mcp\server.py']
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SW_MCP_OUTPUT_ROOT` | `~/Documents/solidworks-mcp` | Every file the server writes stays under here. Paths outside it are refused. |
| `SW_MCP_TEMPLATE_DIR` | auto-discovered | Where to look for `.prtdot` / `.asmdot` / `.drwdot` templates if SOLIDWORKS has no default configured. |
| `SW_MCP_DEMO_TOOLS` | unset | Set to `1` to register the basketball demo tools. Off by default: a `create_basketball` sitting next to `revolve` measurably degrades tool selection. |

---

## The two ideas that make it usable

### 1. Units are explicit, always

Every length parameter ends in `_mm` and every angle in `_deg`. The COM API
underneath is metres and radians; conversion happens once, at the boundary. No
tool has ever silently taken metres.

### 2. Selection is declarative

SOLIDWORKS features read from an implicit global selection set, with per-feature
"marks" deciding which selection means what. Exposing that to an agent is
hopeless. Instead every tool that needs geometry takes the same `selection`
object, and the server sets the marks:

```jsonc
// list_edges first, then:
{ "selection": { "edges": [4, 6, 9] } }
{ "selection": { "face_edges": [2] } }        // every edge of face 2
{ "selection": { "planes": ["front"] } }
{ "selection": { "sketch_segments": [0, 1] } }
{ "selection": { "points": [{ "x_mm": 30, "y_mm": 0, "z_mm": 20, "type": "FACE" }] } }
```

Indices come from `list_faces` / `list_edges` / `list_vertices` /
`list_sketch_segments` and describe the **current** model state — re-list after
any geometry change. Under the hood, selection resolves to a point that provably
lies on the entity and picks by 3D coordinate, which survives the fact that
late-bound Python cannot QueryInterface a `Face2` to `IEntity`.

---

## Tools

### Session and documents
| Tool | Purpose |
| --- | --- |
| `solidworks_status` | Verify the connection; report version and active document. |
| `get_active_document_info` | Title, path, type, unsaved state. |
| `create_new_document` | New part / assembly / drawing from the default template. |
| `open_document` | Open a `.sldprt` / `.sldasm` / `.slddrw`. |
| `save_document` / `save_active_document` | Save-as under the output root / save in place. |
| `export_document` | STEP, IGES, STL, Parasolid, 3MF, an image of the current view, or PDF of a drawing (every sheet, vector, true sheet size; an image of a drawing is only a window-sized capture). |
| `rebuild_document` | Rebuild and report failing features. |
| `set_appearance` / `set_material` | Display colour; real material (so mass properties mean something). |

### Reference geometry
`list_reference_planes`, `create_plane` (offset / angle / midplane / three points /
parallel-through-point), `create_axis`.

### Sketching
`create_sketch` (on a plane **or a model face**), `edit_sketch`, `close_sketch`,
`list_sketches`, `list_sketch_segments`, `get_sketch_status`.

Geometry: `draw_line`, `draw_centerline`, `draw_circle`, `draw_rectangle`,
`draw_arc`, `draw_3point_arc`, `draw_ellipse`, `draw_polygon`, `draw_slot`,
`draw_point`, `draw_spline`.

Editing: `sketch_fillet`, `sketch_chamfer`, `sketch_trim`, `sketch_offset`,
`sketch_mirror`, `convert_entities`, `set_construction_geometry`.

Parametrics: `add_relation`, `add_dimension`, `set_dimension`, `list_dimensions`.

### Features
`boss_extrude`, `cut_extrude`, `revolve`, `fillet`, `chamfer`, `shell`, `draft`,
`rib`, `simple_hole`, `sweep`, `loft`, `linear_pattern`, `circular_pattern`,
`mirror_feature`, `delete_feature`, `rename_feature`, `set_feature_suppression`.

### Sheet metal
| Tool | Purpose |
| --- | --- |
| `sheet_metal_base_flange` | First sheet metal body from a sketch: an open chain of lines is thickened and extruded (each corner a bend), a closed outline becomes a plate. Sets thickness and bend radius for the part. |
| `sheet_metal_edge_flange` | Flange along one or more straight edges, with angle, length from the inner virtual sharp, bend position, radius and relief. The profile sketch is drawn for you. |
| `sheet_metal_miter_flange` | Profile sketch swept along connected edges with mitred corners, rip gap and optional start/end offsets. |
| `sheet_metal_hem` | Closed, open or rolled hem on selected edges. |
| `sheet_metal_closed_corner` / `sheet_metal_break_corner` | Close the corner between two flanges (butt / overlap / underlap with a gap, read back); fillet or chamfer the corners of a sheet edge. |
| `sheet_metal_corner_relief` | Square, circular, obround, tear, bend-waist or constant-width relief where two bends meet, one or more corners per feature. |
| `sheet_metal_flatten` | Unsuppress / suppress the Flat-Pattern feature and report the flat bounding box (single-body parts). |
| `sheet_metal_info` | Thickness, bend radius, K-factor, relief, bend state, and every sheet metal feature with its bends. |
| `export_flat_pattern` | Flat pattern as DXF or DWG with bend lines, without a dialog; a DXF result is summarised (entities, bend lines, extents when the file is in millimetres). |

Every feature-creating tool here is judged by geometry, never by the API's
return value: it reports the body volume and bounding box after the feature,
because several of the sheet metal API calls raise on return even when they
have built the feature, and others return nothing at all when they have not.
What a tool asked for is held against the feature's own readback (thickness,
bend radius, hem length, corner type and gap, ...), a feature that removes
material must have removed some, and a mismatch is `ok: false` with the
feature still in the tree under its reported name, so the caller can inspect
it or hand it to `delete_feature`. Only the leftovers of a call that built
nothing are removed again: a 3D sketch whose lines did not all appear, the
profile sketches of a failed edge flange, a Weldment feature added for a
member that was refused. The boxes of the sheet metal, weldment and
`list_bodies` results come from `IBody2::GetExtremePoint` (six calls per
body, exact on the geometry; for a rotated assembly component `list_bodies`
transforms the two corners, which is exact only without rotation);
`get_bounding_box` keeps `GetPartBox`, which SOLIDWORKS documents as
approximate, so it is for orientation, not for a check.
`sheet_metal_flatten` and `export_flat_pattern` take single-body sheet
metal parts only.

### Weldments
| Tool | Purpose |
| --- | --- |
| `create_3d_sketch` | Straight lines between model-space points, closed as one 3D sketch; the path for structural members. |
| `list_weldment_profiles` | Standard / type / size of every `.sldlfp` profile in the configured folders and the install. |
| `weldment_structural_member` | A library profile swept along connected sketch segments, one body each, corners mitred or butted; adds the Weldment feature when needed. Reports each body's volume and box. |
| `weldment_end_cap` | Plate over the open end of a member, inset by a wall-thickness ratio or a distance, optionally chamfered or inward (the member is cut back); one new body per selected face, every option held against the readback. |
| `weldment_trim_extend` | Trim members flush against other bodies (butt / miter) or cut them at faces and reference planes (trim), with coped cut and weld gap; butt and miter take one member against one body, only trim takes several; reports the trimmed bodies' new boxes and volumes and refuses to call an unchanged model a success. |
| `weldment_gusset` | Triangle or polygon gusset plate between two supporting faces, thickness inner / outer / both sides, plane at start / centre / end of the corner. |

### Inspection — the feedback channel
| Tool | Purpose |
| --- | --- |
| `capture_screenshot` | Returns the view as an **image**, so the model can look at its own work. |
| `list_faces` / `list_edges` / `list_vertices` / `list_bodies` | Topology with types, sizes, and selection indices. Filterable by surface type, area, normal direction, curve type, length. Bodies come with their own volume and the sum. |
| `get_mass_properties` / `get_bounding_box` / `measure` | Numbers to check the geometry against. |
| `check_errors` | What SOLIDWORKS thinks is wrong — see below. |
| `set_view` | Named view plus zoom-to-fit. |

### Assemblies
`list_components`, `insert_component`, `add_mate`, `list_mates`,
`set_component_fixed`.

### Engineering drawings
`create_drawing`, `list_sheets`, `add_sheet`, `activate_sheet`,
`insert_standard_views`, `insert_model_view`, `insert_projected_view`,
`insert_section_view`, `insert_detail_view`, `list_drawing_views`,
`activate_drawing_view`, `create_drawing_sketch`, `set_drawing_view`,
`insert_model_annotations`, `auto_dimension_view`, `insert_center_marks`,
`insert_centerlines`, `add_note`.

`insert_standard_views`, `insert_model_view`, `insert_projected_view` and
`set_drawing_view` take `display_mode`: `hidden_lines_removed` (SOLIDWORKS'
default), `hidden_lines_visible` (hidden edges dashed, so bores and steps show
without a section), `wireframe`, `shaded`, `shaded_with_edges`. The style counts
as applied only when it reads back from the view; otherwise the tool returns
`ok: false` with the style each view ended up in. `insert_standard_views`
applies it only to the views it places.

For section and detail views, call `create_drawing_sketch` after activating the
parent view, then use the normal `draw_line` or `draw_circle` sketch tools.

---

## A worked example

```jsonc
create_new_document   { "kind": "part" }
create_sketch         { "plane": "front", "name": "Base" }
draw_rectangle        { "x1_mm": 10, "y1_mm": 10, "x2_mm": 70, "y2_mm": 50 }
add_dimension         { "selection": { "sketch_segments": [0] }, "kind": "horizontal",
                        "value_mm": 80, "place_x_mm": 40, "place_y_mm": -10 }
add_dimension         { "selection": { "sketch_segments": [1] }, "kind": "vertical",
                        "value_mm": 50, "place_x_mm": -10, "place_y_mm": 30 }
close_sketch          {}
boss_extrude          { "depth_mm": 20, "name": "BasePad" }

list_edges            { "curve_type": "line", "min_length_mm": 19 }   // find the four verticals
fillet                { "radius_mm": 5, "selection": { "edges": [0, 1, 2, 3] } }
shell                 { "thickness_mm": 2, "selection": { "faces": [4] } }

capture_screenshot    { "view": "isometric" }
get_mass_properties   {}
```

---

## Notes from the implementation

These are the things that cost real time. They are documented because anyone
automating SOLIDWORKS from Python will hit them.

**SOLIDWORKS members are inconsistently exposed to pywin32.** Most are declared
in the type library as `PROPGET` *with arguments*. Late-bound pywin32 may resolve
such a member on plain attribute access by invoking it with **no** arguments.
`IBody2.GetFaces` then returns a bound method that looks like a one-element
result — which is why an extruded box reported exactly one face. Worse,
`ModelDocExtension.AddDimension` invoked that way **crashes SOLIDWORKS outright**.
`sw_core.flag_methods()` calls `_FlagAsMethod` on every member the server invokes
with arguments, and `sw_core.value()` invokes zero-argument callables rather than
returning the method object.

**Modal dialogs deadlock the server.** `AddDimension2` pops the "Modify" box
whenever *Input dimension value* is enabled — which is the default. A modal
dialog blocks the COM call that opened it, and with it the whole server, until
somebody clicks. Dimension tools turn the preference off and restore it after.
If a tool ever hangs, look for a dialog behind the SOLIDWORKS window.

**Return values lie.** `SketchTrim` returns `False` on success. The `Create*`
sketch APIs return arrays whose truthiness is unreliable. `SketchAddConstraints`
takes a magic string and *silently ignores* one it does not recognise. So the
drawing tools judge success by the sketch's segment count, `sketch_trim` by
whether the sketch actually changed, and `add_relation` uses the typed
`ISketchRelationManager.AddRelation` and verifies via the relation count — and on
failure reports which relations that selection *would* accept.

**Most failures are silent.** SOLIDWORKS returns null far more often than it
raises. Every feature tool therefore rebuilds and reports
`ModelDocExtension.GetWhatsWrong`, and `check_errors` exposes it directly.

**`sketch_trim` needs its target selected.** Pass the segment in `selection`;
the point alone only tells SOLIDWORKS which piece to discard.

**Multi-reference features want distinct marks.** `InsertRefPlane` reads its
first, second, and third reference from *marks 0, 1, 2* — not from selection
order. Selecting all of them with mark 0 silently produces nothing, which is why
`create_plane` selects each reference with its own mark and documents that
references are consumed in written order.

**Two unit traps.** `IPartDoc.GetPartBox(False)` returns *document* units, not
metres, so it reads 1000x large if you treat it like the rest of the API; pass
`True`. And `swSketchLINE` is `0`, so folding a segment type through `or` turns
every line into "unknown".

## Known limitations

- One SOLIDWORKS session, one COM apartment: tool calls are serialised. Two
  clients driving the same session at once will deadlock it.
- Snapshot indices from `list_faces` / `list_edges` are invalidated by any
  geometry change. Re-list; do not cache.
- A modal dialog opened by SOLIDWORKS for any *other* reason will still block
  the server until dismissed.
- The sheet metal tools work on single-body sheet metal parts. A multibody
  sheet metal part has one Flat-Pattern feature per body; `sheet_metal_flatten`
  and `export_flat_pattern` refuse such a part instead of reporting one body
  as the whole.
- `rib` is picky about its profile. `InsertRib` returns void and simply builds
  nothing unless the open profile reaches material at both ends *and* the
  extrusion direction can get there. The tool defaults to `parallel_to_sketch`
  (right for a cross-section profile) and retries the other direction
  automatically, reporting which one worked. A profile that overlaps an existing
  rib still fails, and the tool says so rather than pretending.
- Only the **first** `insert_projected_view` off a given parent succeeds;
  subsequent projections from the same parent return nothing, through
  re-activation and rebuild alike. Use `insert_standard_views` for a full
  orthographic set.
- Detail-view scale is **absolute** (model to paper), not relative to the parent.
  On a 1:4 sheet, `2:1` makes the detail eight times the parent view and it
  overflows the sheet. Pick a scale near the sheet scale.
- Standard-plane and model-view display names are localized. Use `front`,
  `top`, and `right` for reference planes, and use names returned by
  `list_drawing_views` for drawing views; do not hard-code English UI labels.
- `through_all_both` is translated internally to a two-direction feature with
  `through_all` in both directions. Passing `swEndCondThroughAllBoth` as a
  single-ended feature silently cuts only one side in SOLIDWORKS 2026.
- `circular_pattern` cannot pattern a feature that built a *separate* body
  (`merge: false`) — SOLIDWORKS rejects the feature scope. Merge the body, or
  pattern the body instead.
- `save_document` cannot overwrite a file SOLIDWORKS currently has open, even
  with `overwrite: true`. The error says so when that is the cause.
- Sheet metal (measured on 2016 SP3): the K-factor cannot be changed through
  the API — `ISheetMetalFeatureData` and `ICustomBendAllowance` accept the
  value and keep the document default — so `sheet_metal_info` reports what the
  flat pattern actually uses. `tear_drop` and `double` hems are not accepted.
  With the profile this tool draws, `InsertSheetMetalEdgeFlange2` builds the
  same flange whatever dimension type it is given: the outer virtual sharp
  reads back `length + t·tan(angle/2)` for the same geometry, and the bend
  tangent builds nothing; so `sheet_metal_edge_flange` offers no length
  reference and measures from the inner virtual sharp. Options that the
  feature definitions do not read back on 2016 (all measured 2026-10-08),
  which are therefore passed and left to the geometry: `flip` and
  `relief_type` of the edge flange, `trim_side_bends` of the miter flange,
  `depth_mm` of the base flange, `position` of the hem, every parameter of
  the corner relief (its feature has no definition at all), `gap_mm` and
  `coped_cut` of the trim, `thickness_direction` and `location` of the
  gusset. The end cap's reverse flag is not offered: it reads back as set and
  moves nothing.
  `export_flat_pattern` needs a saved part, because `ExportToDWG2` takes the
  model path; `ExportFlatPatternView` is not used because it opens a file
  dialog. A gauge table is refused on this install's templates
  (`SetUseGaugeTable` answers "not enabled on template"), and the 2016
  material database carries no sheet metal parameters, so neither route sets
  the K-factor either.
- Weldments: `weldment_trim_extend` against a body shortens the trimmed member
  and grows the trimming member by the same amount, whatever the API's
  extension option bits say (measured with every combination on 2016), so the
  part's `get_mass_properties` does not move and the tool offers no extension
  switch; `list_bodies` and the weldment tools report each body's own volume for
  that reason (the part total is the plain sum of its bodies, overlaps
  included: two 20 x 20 x 10 boxes overlapping by half report 8000 mm³). The
  gusset's weld-bead chamfer and sketch-plane offset arguments of
  `InsertGussetFeature3` are accepted and change nothing on 2016, so
  `weldment_gusset` does not offer them.

## Testing

The useful tests are live SOLIDWORKS tests: the COM behaviour that matters here
cannot be mocked faithfully. With SOLIDWORKS already running, run:

```powershell
.\.venv\Scripts\python.exe tests\live_p0_regression.py
```

It verifies the P0 geometry/constraint regressions, including the full-volume
`through_all_both` cut. `tests\live_sheet_metal.py` builds an L profile and a
plate with every sheet metal tool and checks the flanges, the flat pattern and
the break corner against a closed-form number: flange volumes, the developed
length from the K-factor, the material a break corner removes, the bend lines
in the exported DXF; the hem, the closed corner and the corner relief are
checked by readback, added or removed material and the DXF (32 checks on
2016 SP3, plus 11 for the third part). `tests\live_weldment.py` does the same for weldments: members
measure profile area times length and reach the mitred outer corner, the end
cap its inset plate, the trimmed member its new length, the gussets a*b*t/2
with their leg lengths and the polygon with its corner cut, a plane cut into
two halves, and a degenerate 3D line leaves no sketch behind; a second pair
with butt corners, a turned and mirrored profile and a gap, and end caps with
ratio inset and chamfer (volume), and inset by distance and inward (the
plate ends flush inside, the recut member is reported), are checked by
readback and geometry (41 checks). A third sheet metal part checks an edge flange and a miter flange
with every option off its default by readback and reach.

Every tool has been exercised against SOLIDWORKS 2026 SP3.2 on a Simplified
Chinese install. Where a result could be checked numerically it was: the revolved
ring, swept rod and lofted cone match their closed-form volumes; a 5-degree
`draft` removes exactly the expected wedge; `rib` produces exactly the triangle
under its profile; `through_all_both` removes the full cylinder rather than half
of it. The limitations above are what survived that pass.

Older releases work too, back to at least SOLIDWORKS 2016 SP3, where every tool
was exercised the same way on a German install. Where a release lacks the newest
numbered method (`FeatureCut4`, `FeatureLinearPattern5`, `CreateDetailViewAt4`,
...) the tool falls back to the earlier variant with the arguments it takes; on
newer releases the newest name is always tried first, so nothing changes there.
The sheet metal and weldment tools are the exception: they were measured on
2016 SP3 only and call the API names that build there (`InsertSheetMetalBaseFlange`,
`InsertSheetMetalHem`, `InsertStructuralWeldment4`, `InsertEndCapFeature3`, ...),
without a newest-name-first fallback.

## Layout

| Module | Contents |
| --- | --- |
| `sw_core.py` | COM attachment, units, feature tree, topology enumeration, selection engine, tool registry |
| `sw_file.py` | Session status, documents, saving, exporting, appearance, material |
| `sw_refgeom.py` | Reference planes and axes |
| `sw_sketch.py` | Sketches, geometry, editing, relations, dimensions |
| `sw_feature.py` | Solid features |
| `sw_sheetmetal.py` | Sheet metal features, flat pattern, DXF/DWG export |
| `sw_weldment.py` | 3D sketches, structural members, end caps, trim/extend, gussets |
| `sw_inspect.py` | Topology listings, measurement, mass properties, screenshots |
| `sw_assembly.py` | Components and mates |
| `sw_drawing.py` | Sheets, views, model items, dimensions, center marks, notes |
| `sw_demo.py` | Basketball demo, opt-in via `SW_MCP_DEMO_TOOLS` |
| `tests/live_p0_regression.py` | Live regression checks for the confirmed P0 part/sketch defects |
| `tests/live_sheet_metal.py` | Live sheet metal checks against closed-form geometry |
| `tests/live_weldment.py` | Live weldment checks against closed-form geometry |
| `tools/tlb_probe.py` | Reads signatures and enums straight off your installed type library |
| `server.py` | Registry assembly and stdio dispatch |

`tools/tlb_probe.py` is worth knowing about before you add a tool. The published
API reference disagrees with the shipped type library often enough to matter —
`FeatureFillet3` takes 14 arguments here, not the documented 7 — so check first:

```bash
python tools/tlb_probe.py methods '^IFeatureManager$' '^FeatureFillet3$'
python tools/tlb_probe.py enums '^swMateType_e$'
```

Adding a tool means writing one decorated function; the registry does the rest.

```python
@tool("my_tool", "What it does. Lengths are millimetres.",
      {"depth_mm": {"type": "number"}}, ["depth_mm"])
def my_tool(args: dict[str, Any]) -> dict[str, Any]:
    _, doc = require_part()
    ...
    return result(True, "Did the thing.")
```

---

## License

Apache License 2.0 — see [LICENSE](LICENSE). Copyright 2026 JIALE LIU; modifications Copyright 2026 Christian Dengler (d-veloping), see [NOTICE](NOTICE).

Use it, change it, ship it in a commercial product; that is all allowed. What
the license does require, if you redistribute this or anything derived from it,
is that you keep the copyright notices, pass on a copy of the [NOTICE](NOTICE)
file, and state which files you changed.
