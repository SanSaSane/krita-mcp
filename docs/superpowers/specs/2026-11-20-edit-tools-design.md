# Edit tools for the Krita MCP bridge

Status: draft, 2026-11-20. Fork of [SanSaSane/krita-mcp](https://github.com/SanSaSane/krita-mcp) (CC0), branch `edit-tools`.

## Why

The bridge is used to prepare cut-paper character art for a 2D rig: pieces are cut from generated images, then moved, scaled and turned until they match a reference pose, recoloured to an exact palette, and exported one PNG per piece. The owner describes an edit in words ("move the cap 12 px left, turn the front forearm 8°"), Claude applies it to the live document, looks at the result with `get_image`, and the owner finishes fine work by hand.

Upstream covers documents, layers, drawing, filters and viewing. It has no per-layer transform, no cut/copy/paste, only rectangular selections, and its pixel writes bypass Krita's undo history.

## Goals

- Move, scale, rotate and flip one layer around a chosen pivot.
- Select by rectangle, ellipse, polygon, colour, or a layer's opaque area.
- Cut, copy and clear a selection; lift a selection onto a new layer.
- Fill and replace colours exactly (palette clean-up).
- Every edit can be reverted, either through Krita's undo or a snapshot the bridge keeps.
- Measure pieces (opaque bounds, difference against a reference layer) and export each layer as a trimmed PNG plus a JSON file with positions and pivots.

## Non-goals

- Brush painting, animation, vector editing.
- Unattended batch pipelines. Those stay as plain scripts outside Krita.
- Supporting colour spaces other than RGBA/8-bit for pixel edits (upstream's limit).

## Anti-goals

- An edit that silently cannot be undone.
- Tools that act on "whatever is active" without saying which layer they touched.

## Constraints

- Krita 5.3.x, PyQt5, libkis only; MCP server stays standard-library only.
- macOS and Windows. Upstream was only run on Windows.
- Keep upstream's safety model: loopback only, per-session token.
- Keep changes additive so upstream fixes can still be merged.

## Approaches

1. **Fork upstream and add operations (chosen).** Its GUI-thread hand-off, auth, discovery and 77-check suite are the hard parts and already exist.
2. Use upstream unchanged and do every edit through `run_python`. No build cost, but each edit is fresh ad-hoc code: slower, more error-prone, nothing tested. Used only for the spike.
3. Write a new server. Rebuilds the same plumbing for no gain. nanayax3/krita-mcp offers only brush strokes, has no auth and no tests, so there is nothing to merge from it.

## Design

New operations in `pykrita/krita_mcp/ops.py` (split into `ops_edit.py` if the file grows past comfort), each with an MCP tool entry in `mcp_server.py`:

| Tool | Does |
| --- | --- |
| `transform_layer` | `move` (dx, dy or absolute), `scale`, `rotate` (degrees), `flip`; `pivot` as a point or a named anchor (centre, corners of the opaque bounds). `mode`: `apply` (pixels change) or `mask` (non-destructive transform mask) |
| `set_selection` (extended) | adds `ellipse`, `polygon`, `color` (tolerance, contiguous or global), `layer_alpha` |
| `edit_selection` | `cut`, `copy`, `clear`, `to_new_layer`; `paste` at a position |
| `fill` | fill the selection or a flood region with one colour; `replace_color` maps one colour (with tolerance) to another |
| `import_layer` | add an image file as a new layer at a position |
| `snapshot` / `restore` | keep and bring back a hidden copy of a layer; taken automatically before any edit Krita's undo does not record |
| `measure` | opaque bounds, centre, pixel count per layer; difference against a reference layer |
| `export_layers` | one trimmed PNG per layer plus a JSON manifest (name, offset, size, pivot) |

Every mutating tool returns the layer it touched, its bounds before and after, and `undoable: true|false`.

## Open until the spike

- Pivot and undo behaviour of libkis `Node.rotateNode` / `scaleNode` / `move`.
- Whether `Selection.cut/copy/paste` work with Krita's window unfocused.

If libkis does not give pivot control or undo, transforms are computed with `QTransform` on the layer's pixels and written back, with an automatic snapshot.

## Testing

Extend `test_mcp.py` with pixel-exact checks for each new tool on a scratch document. Run the full suite on macOS before each commit; run once on Windows before calling it done.
