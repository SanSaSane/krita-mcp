# Edit tools for the Krita MCP bridge

Status: built, 2026-11-20. What the spike decided is at the end. Fork of [SanSaSane/krita-mcp](https://github.com/SanSaSane/krita-mcp) (CC0), branch `edit-tools`.

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

New operations in `pykrita/krita_mcp/ops_edit.py`, registered into `ops.OPS` at import so `ops.py` stays close to upstream, each with an MCP tool entry in `mcp_server.py`:

| Tool | Does |
| --- | --- |
| `transform_layer` | `translate`, `move_to`, `scale`, `rotate` (degrees, clockwise), `flip`; `pivot` as a point or a named anchor of the opaque bounds |
| `set_selection` (extended) | adds `ellipse`, `polygon`, `color` (tolerance, contiguous or global), `layer_alpha`, and `combine` |
| `edit_selection` | `clear`, `fill`, `copy_to_layer`, `cut_to_layer` (optionally at another position or into another open document) |
| `replace_color` | maps one colour (with tolerance) to another, keeping each pixel's opacity |
| `import_layer` | add an image file as a new layer at a position |
| `snapshot` | `list`, `take`, `restore`, `drop`; a compressed in-memory copy of a layer, taken automatically before every edit |
| `measure` | opaque bounds, centre, pixel count per layer; difference against a reference layer |
| `export_layers` | one trimmed PNG per layer plus a JSON manifest (name, offset, size, pivot) |

Every mutating tool returns the layer it touched, its bounds before and after, and `undoable: true|false`.

## What the spike decided

Run on macOS, Krita 5.3.4, on a scratch document.

| Question | Found | Decision |
| --- | --- | --- |
| `Node.move` | Works, lossless; not in the undo history | Used for whole-pixel moves |
| `Node.rotateNode` / `scaleNode` | Work and are undoable, but run asynchronously (the result is only there after the event loop has turned) and rotate about the layer's own centre only | Not used. Transforms are computed with `QTransform` on the layer's pixels: any pivot, synchronous, one code path for scale, rotate and flip |
| `Selection.cut` | Works | Not used, for consistency with paste |
| `Selection.paste` | Pasted nothing | Cut and copy read and write pixels directly through the selection mask |
| Undo | Mixed: some libkis calls are recorded, pixel writes and `move` are not | One rule instead: every edit takes a snapshot first and says `undoable: false` |

Left out of this version: the non-destructive transform mask mode (`mode: mask`). Resampling an already resampled layer softens it, so for now the way to adjust a transform is to restore its snapshot and apply the corrected values.

## Testing

`test_edit.py` holds pixel-exact checks for each new tool on a scratch document (44 checks), reusing the client from `test_mcp.py`. Run both suites on macOS before each commit; run once on Windows before calling it done.
