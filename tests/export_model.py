"""Explicitly limited local model of a real native export request (M004-S19, test-only).

It interprets ONLY the export-boundary operations that the actual request expression places
above the science product, on one window of the recipe grid. It is not an Earth Engine emulator
and nothing it returns is server evidence. Supported operations, in whatever order the request
has them:

- Image.clip(geometry): the recipe AOI (mask = the caller's per-cell AOI coverage) or a
  GeometryConstructors.Rectangle in exactly the recipe grid projection (mask 1 on cells it fully
  contains, 0 on cells it does not touch; a partially covered cell is refused);
- Image.reproject(crs, crsTransform) to exactly the recipe grid: identity on that grid;
- Image.toDouble: float64 values;
- Image.unmask(Image.constant(v), sameFootprint=False): mask-0 cells take v with mask 1.

The walk stops at the first other operation; that node is the science product, supplied as
per-band (values, mask) arrays on the window. Masks combine by minimum, which leaves the
mask > 0 validity used below unchanged. The outermost operation must be a clip, which sets the
delivered extent: a rectangle must be exactly the expected window; for the AOI polygon the
caller's expected window is its enclosing window by the audit's own definition.

Writer: the observed execution181 symptom, not proven server behaviour. A cell whose final mask
is 0 is delivered as NaN; every other cell is delivered as its value.
"""

import json

import ee
import numpy as np

SUPPORTED = {"Image.clip": "input", "Image.reproject": "image", "Image.toDouble": "value",
             "Image.unmask": "input"}


class Unsupported(AssertionError):
    """The request contains something this model deliberately does not interpret."""


def name(node):
    return node.func.getSignature()["name"] if isinstance(node, ee.ComputedObject) and node.func else None


def chain(expression):
    """Supported calls from the outermost inward, and the first other node (the product)."""
    calls, node = [], expression
    while name(node) in SUPPORTED:
        calls.append((name(node), node.args))
        node = node.args[SUPPORTED[name(node)]]
    return calls, node


def _root(value):
    graph = json.loads(value.serialize())
    return graph["values"][graph["result"]]


def _plain(value):
    """A constant argument as plain JSON data (native objects and decoded requests alike)."""
    if not isinstance(value, ee.ComputedObject):
        return value
    node = _root(value)
    if "arrayValue" in node:
        return [x["constantValue"] for x in node["arrayValue"]["values"]]
    return node["constantValue"]


def rectangle(geometry, grid):
    """[x0, y0, x1, y1] grid-pixel coordinates of a planar Rectangle in exactly the grid projection."""
    call = _root(geometry).get("functionInvocationValue") or {}
    args = call.get("arguments", {})
    crs = args.get("crs", {}).get("functionInvocationValue", {})
    if (call.get("functionName") != "GeometryConstructors.Rectangle"
            or set(args) != {"coordinates", "crs", "geodesic"}
            or args["geodesic"].get("constantValue") is not False
            or crs.get("functionName") != "Projection"
            or crs["arguments"].get("crs", {}).get("constantValue") != grid.crs
            or crs["arguments"].get("transform", {}).get("constantValue") != list(grid.transform)):
        raise Unsupported("clip geometry is neither the AOI nor a grid-pixel rectangle")
    return args["coordinates"]["constantValue"]


def _clip_mask(geometry, grid, window, aoi, coverage):
    if geometry.serialize() == aoi.serialize():
        return np.asarray(coverage, float)
    x0, y0, x1, y1 = rectangle(geometry, grid)
    cols = np.arange(window["width"]) + window["col_off"]
    rows = np.arange(window["height"]) + window["row_off"]
    inside = ((cols >= x0) & (cols + 1 <= x1))[None, :] & ((rows >= y0) & (rows + 1 <= y1))[:, None]
    apart = ((cols + 1 <= x0) | (cols >= x1))[None, :] | ((rows + 1 <= y0) | (rows >= y1))[:, None]
    if not (inside | apart).all():
        raise Unsupported("rectangle partially covers window cells")
    return inside.astype(float)


def _fill(args):
    value = _root(args["value"]).get("functionInvocationValue") or {}
    if value.get("functionName") != "Image.constant" or args.get("sameFootprint") is not False:
        raise Unsupported("only unmask(Image.constant(v), sameFootprint=False) is modelled")
    return float(value["arguments"]["value"]["constantValue"])


def deliver(expression, *, grid, window, aoi, coverage, bands):
    """Model the request over per-band (values, mask) science arrays shaped like the window.

    Returns (delivered float64 array [band, row, col], product node, outermost-first call names)."""
    calls, product = chain(expression)
    if not calls or calls[0][0] != "Image.clip":
        raise Unsupported("the outermost operation must bound the extent with a clip")
    geometry = calls[0][1]["geometry"]
    if geometry.serialize() != aoi.serialize():
        expected = [window["col_off"], window["row_off"], window["col_off"] + window["width"],
                    window["row_off"] + window["height"]]
        if rectangle(geometry, grid) != expected:
            raise AssertionError(f"delivered extent {rectangle(geometry, grid)} is not the window {expected}")
    values = np.array([np.asarray(v, float) for v, _ in bands])
    mask = np.array([np.asarray(m, float) for _, m in bands])
    if values.shape[1:] != (window["height"], window["width"]) or mask.shape != values.shape:
        raise Unsupported("science arrays must cover exactly the window")
    for op, args in reversed(calls):
        if op == "Image.clip":
            mask = np.minimum(mask, _clip_mask(args["geometry"], grid, window, aoi, coverage)[None])
        elif op == "Image.reproject":
            if (args.get("scale") is not None
                    or args["crs"].serialize() != ee.Projection(grid.crs).serialize()
                    or _plain(args["crsTransform"]) != list(grid.transform)):
                raise Unsupported("only reprojection onto exactly the recipe grid is modelled")
        elif op == "Image.toDouble":
            values = values.astype(np.float64)
        else:
            missing = mask == 0
            values = np.where(missing, _fill(args), values)
            mask = np.where(missing, 1.0, mask)
    return np.where(mask > 0, values, np.nan), product, [op for op, _ in calls]
