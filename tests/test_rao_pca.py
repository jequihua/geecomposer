"""GN/PCA coordinates are explicit dot products of standardized bands (M004-S11).

Graph checks use the installed EE client offline (``real_ee``); nothing is evaluated.
Numeric checks run production ``RaoNormalization.apply`` on ``CommonMaskPixels``, a
test-only adapter limited to the case observed in the M004-S04 diagnostic: all bands
of a pixel share one (possibly fractional) mask, and a band ``reduce(Reducer.sum())``
returned the sum scaled by that mask. It is not an Earth Engine emulator and says
nothing about other masks, reducers or server behavior.
"""

from types import SimpleNamespace

import ee
import numpy as np
import pytest

from geecomposer.rao import RaoNormalization
from ee_graph import Graph

OBSERVED_HALF = 0.501960814  # approximate half-mask value observed on the tested inputs


def model(bands, loadings, means=None, scales=None):
    p = len(bands)
    return RaoNormalization(
        bands=bands,
        means=means or [10.0 * (i + 1) for i in range(p)],
        scales=scales or [2.0 + i for i in range(p)],
        loadings=loadings,
        retained_fraction=1.0,
        n_sigmas=6.0,
        sample_count=10,
        decimal_places=None,
        calibration="test",
    )


S = np.sqrt(0.5)
CASES = {
    "scalar": (("v",), ((1.0,),)),
    "scalar_signed": (("v",), ((-1.0,),)),
    "two_band_rotation": (("a", "b"), ((S, S), (-S, S))),
    "three_band_signed_zero": (("B4", "B8", "B11"), ((0.6, 0.8, 0.0), (-0.8, 0.6, 0.0), (0.0, 0.0, 1.0))),
    "retained_subset": (("x", "y", "z"), ((0.0, -1.0, 0.0),)),
}


def expected(m, values):
    """Independent NumPy standardization and matrix dot product, one row per pixel."""
    z = (np.asarray(values, float) - np.asarray(m.means)) / np.asarray(m.scales)
    return z @ np.asarray(m.loadings).T


# ------------------------------------------------------------------ installed-client graph ancestry


def call(graph, node):
    node = graph.resolve(node)
    inv = node["functionInvocationValue"]
    return inv["functionName"], inv["arguments"]


def terms(graph, node):
    """Flatten a component into (band index, weight, standardized-input node) products."""
    name, args = call(graph, node)
    if name == "Image.rename":
        return terms(graph, args["input"])
    if name == "Image.add":
        return terms(graph, args["image1"]) + terms(graph, args["image2"])
    assert name == "Image.multiply", name
    sel_name, sel = call(graph, args["image1"])
    const_name, const = call(graph, args["image2"])
    assert (sel_name, const_name) == ("Image.select", "Image.constant")
    (index,) = graph.constant(sel["bandSelectors"])
    return [(index, graph.constant(const["value"]), graph.resolve(sel["input"]))]


@pytest.mark.parametrize("case", CASES)
def test_apply_graph_is_explicit_ordered_dot_product(real_ee, case):
    bands, loadings = CASES[case]
    m = model(bands, loadings)
    reordered = list(reversed(bands)) + ["extra"]
    image = ee.Image.constant(list(range(len(reordered)))).rename(reordered)
    graph = Graph(m.apply(image))
    assert call(graph, graph.root)[0] == "Image.toDouble"
    for forbidden in ("Image.reduce", "Image.updateMask", "Image.mask", "Image.unmask", "Image.clip",
                      "Image.where"):
        assert not list(graph.calls(forbidden)), forbidden
    renames = {tuple(graph.constant(a["names"])): a for a in graph.calls("Image.rename")
               if graph.constant(a["names"])[0].startswith("pc_")}
    assert set(renames) == {(f"pc_{i}",) for i in range(len(loadings))}
    inputs = set()
    for i, row in enumerate(loadings):
        found = terms(graph, renames[(f"pc_{i}",)]["input"])
        assert [(k, w) for k, w, _ in found] == [(k, row[k]) for k in range(len(bands))]
        inputs |= {id(node) for _, _, node in found}
        standardized = found[0][2]
    assert len(inputs) == 1  # every product uses the same standardized image
    name, divide = call(graph, standardized)
    assert name == "Image.divide" and graph.constant(call(graph, divide["image2"])[1]["value"]) == list(m.scales)
    name, subtract = call(graph, divide["image1"])
    assert name == "Image.subtract" and graph.constant(call(graph, subtract["image2"])[1]["value"]) == list(m.means)
    name, select = call(graph, subtract["image1"])
    assert name == "Image.select" and [graph.constant(x) for x in select["bandSelectors"]["arrayValue"]["values"]] \
        == list(bands)


# ------------------------------------------------------------------ observed common-mask adapter


class CommonMaskPixels:
    """Pixels x bands values with ONE mask per pixel shared by all bands (observed case only)."""

    def __init__(self, data, mask=None, names=None):
        if isinstance(data, CommonMaskPixels):
            self.data, self.pixel_mask, self.names = data.data, data.pixel_mask, data.names
            return
        self.data = np.atleast_2d(np.asarray(data, float))
        self.pixel_mask = np.ones(len(self.data)) if mask is None else np.asarray(mask, float)
        self.names = names or [f"b{i}" for i in range(self.data.shape[1])]

    @staticmethod
    def constant(values):
        return CommonMaskPixels([np.atleast_1d(np.asarray(values, float))])

    @staticmethod
    def cat(images):
        return CommonMaskPixels(np.hstack([i.data for i in images]), images[0].pixel_mask,
                                [n for i in images for n in i.names])

    def select(self, bands):
        bands = [bands] if isinstance(bands, (int, str)) else list(bands)
        idx = [b if isinstance(b, int) else self.names.index(b) for b in bands]
        return CommonMaskPixels(self.data[:, idx], self.pixel_mask, [self.names[i] for i in idx])

    def _op(self, other, fn):
        other = other if isinstance(other, CommonMaskPixels) else CommonMaskPixels.constant(other)
        mask = np.minimum(self.pixel_mask, other.pixel_mask) if len(other.data) == len(self.data) else self.pixel_mask
        return CommonMaskPixels(fn(self.data, other.data), mask, self.names)

    def subtract(self, other):
        return self._op(other, np.subtract)

    def divide(self, other):
        return self._op(other, np.divide)

    def multiply(self, other):
        return self._op(other, np.multiply)

    def add(self, other):
        return self._op(other, np.add)

    def reduce(self, reducer):
        assert reducer == "sum"
        # observed on the diagnostic inputs: the band sum scaled by the pixel's (common) mask
        return CommonMaskPixels((self.data.sum(axis=1) * self.pixel_mask)[:, None], self.pixel_mask, ["sum"])

    def rename(self, names):
        return CommonMaskPixels(self.data, self.pixel_mask, [names] if isinstance(names, str) else list(names))

    def toDouble(self):
        return self


@pytest.fixture
def pixels(monkeypatch):
    monkeypatch.setattr(ee, "Image", CommonMaskPixels)
    monkeypatch.setattr(ee, "Reducer", SimpleNamespace(sum=lambda: "sum"))
    return CommonMaskPixels


def test_adapter_reproduces_observed_reducer_scaling(pixels):
    """Control: the adapter's reducer model is what the regression guards against."""
    out = pixels([[2.0, 4.0]], mask=[OBSERVED_HALF]).reduce("sum")
    assert out.data[0, 0] == pytest.approx(6.0 * OBSERVED_HALF, rel=1e-15)


MASKS = {"binary": 1.0, "observed_half": OBSERVED_HALF, "quarter": 0.25, "high": 0.9}


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("mask", MASKS)
def test_coordinates_equal_numpy_dot_product_under_common_masks(pixels, case, mask):
    bands, loadings = CASES[case]
    m = model(bands, loadings)
    rng = np.random.default_rng(len(bands) * 7 + len(loadings))
    values = rng.normal(size=(4, len(bands))) * 5 + np.asarray(m.means)
    order = list(reversed(bands))  # input band order differs from the model's
    image = pixels(values[:, ::-1], mask=[MASKS[mask]] * 4, names=order)
    out = m.apply(image)
    want = expected(m, values)
    assert out.names == [f"pc_{i}" for i in range(len(loadings))]
    assert list(out.pixel_mask) == [MASKS[mask]] * 4  # adapter-level mask tracking only
    for p in range(4):
        for i in range(len(loadings)):
            assert out.data[p, i] == pytest.approx(want[p, i], rel=1e-12, abs=1e-12)


def test_constant_input_at_means_is_zero_and_missing_pixel_stays_masked(pixels):
    bands, loadings = CASES["three_band_signed_zero"]
    m = model(bands, loadings)
    values = np.array([m.means, [13.0, 22.0, 31.0]])
    out = m.apply(pixels(values, mask=[OBSERVED_HALF, 0.0], names=list(bands)))
    assert list(out.data[0]) == [0.0, 0.0, 0.0]
    assert out.pixel_mask[1] == 0.0  # missing stays invalid; no mask manipulation in apply
    want = expected(m, values)
    for i in range(3):
        assert out.data[1, i] == pytest.approx(want[1, i], rel=1e-12)


def test_mixed_pixel_masks_each_match_numpy(pixels):
    bands, loadings = CASES["three_band_signed_zero"]
    m = model(bands, loadings)
    values = np.array([[400.0, 3000.0, 1900.0], [250.0, 3900.0, 1850.0], [610.0, 2800.0, 2100.0]])
    out = m.apply(pixels(values, mask=[1.0, OBSERVED_HALF, 0.2078431397676468], names=list(bands)))
    want = expected(m, values)
    for p in range(3):
        for i in range(3):
            assert out.data[p, i] == pytest.approx(want[p, i], rel=1e-12, abs=1e-12)
