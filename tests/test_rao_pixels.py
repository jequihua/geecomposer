"""Production arithmetic executed on arrays; independent pixel oracle."""

from types import SimpleNamespace

import ee
import numpy as np
import pytest

from geecomposer.rao import RaoGrid, RaoNormalization, quantize, rao_q
from rao_raster_double import Raster, Collection
from test_rao import oracle, fit


@pytest.fixture
def raster_api(monkeypatch):
    monkeypatch.setattr(ee, "Image", Raster)
    monkeypatch.setattr(ee, "ImageCollection", Collection)
    monkeypatch.setattr(
        ee, "Reducer", SimpleNamespace(min=lambda: "min", sum=lambda: "sum")
    )
    monkeypatch.setattr(ee, "Kernel", SimpleNamespace(square=lambda r, *_: r))
    monkeypatch.setattr(RaoGrid, "projection", lambda *_: None)


@pytest.mark.parametrize("dimensions", [1, 2])
@pytest.mark.parametrize("missing", ["strict", "valid_subset"])
@pytest.mark.parametrize("normalized", [False, True])
def test_production_pixels_against_independent_oracle(
    raster_api, dimensions, missing, normalized
):
    values = np.arange(25 * dimensions, dtype=float).reshape(5, 5, dimensions) / 10 - 2
    valid = np.ones_like(values, dtype=bool)
    valid[1, 1, -1] = False
    values[3, 3, 0] = np.nan
    values[1, 3, 0] = np.inf
    bands = [f"b{i}" for i in range(dimensions)]
    samples = np.arange(20 * dimensions, dtype=float).reshape(20, dimensions) / 10
    model = fit(samples) if normalized else None
    kwargs = dict(min_valid=4) if missing == "valid_subset" else {}
    result = rao_q(
        Raster(values, valid, bands),
        bands,
        grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
        model=model,
        missing=missing,
        **kwargs,
    )
    common = np.all(valid & np.isfinite(values), axis=2)
    for y in range(5):
        for x in range(5):
            neighbors = [
                values[j, i]
                for j in range(max(0, y - 1), min(5, y + 2))
                for i in range(max(0, x - 1), min(5, x + 2))
                if common[j, i]
            ]
            n = len(neighbors)
            assert result.data[y, x, 1] == n
            assert result.data[y, x, 2] == pytest.approx(n / 9)
            expected_valid = common[y, x] and n >= (9 if missing == "strict" else 4)
            assert result.valid[y, x, 0] == expected_valid
            if expected_valid:
                vectors = np.array(neighbors)
                if model:
                    vectors = (
                        (vectors - np.array(model.means)) / model.scales
                    ) @ np.array(model.loadings).T
                expected = oracle(vectors) / (model.bound if model else 1)
                assert result.data[y, x, 0] == pytest.approx(
                    expected, abs=1e-10, rel=1e-8
                )


def test_rounding_negative_and_positive_ties(raster_api):
    values = np.array([-0.625, -0.375, -0.125, 0.125, 0.375, 0.625])
    result = quantize(Raster(values), 2)
    np.testing.assert_array_equal(
        result.data.ravel(), [-0.62, -0.38, -0.12, 0.12, 0.38, 0.62]
    )


def test_valid_constant_is_zero_missing_window_is_not(raster_api):
    values = np.ones((5, 5, 1))
    result = rao_q(
        Raster(values), ["b0"], grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0))
    )
    assert result.valid[2, 2, 0]
    assert result.data[2, 2, 0] == 0
    assert not result.valid[0, 0, 0]


def test_aoi_boundary_oracle_and_processing_halo(raster_api):
    values = np.arange(121, dtype=float).reshape(11, 11)
    region = np.zeros((11, 11), dtype=bool)
    region[1:10, 1:10] = True
    model = fit(values[region])
    grid = RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0))
    full = rao_q(Raster(values), ["b0"], grid=grid, region=region, model=model)
    for y, x in np.ndindex(region.shape):
        assert full.valid[y, x, 1] == region[y, x]
        assert full.valid[y, x, 2] == region[y, x]
        if not region[y, x]:
            assert not full.valid[y, x, 0]
            continue
        neighborhood = values[y - 1 : y + 2, x - 1 : x + 2]
        mask = region[y - 1 : y + 2, x - 1 : x + 2]
        n = int(mask.sum())
        assert full.data[y, x, 1] == n
        assert full.data[y, x, 2] == pytest.approx(n / 9)
        assert full.valid[y, x, 0] == (n == 9)
        if n == 9:
            expected = (
                oracle((neighborhood.ravel() - model.means[0]) / model.scales[0]) / 12
            )
            assert full.data[y, x, 0] == pytest.approx(expected)
    # A processing tile reads one cell of halo but uses the full analysis AOI.
    support = np.zeros_like(region)
    support[3:9, 3:9] = True
    tiled = rao_q(
        Raster(values, support[..., None]),
        ["b0"],
        grid=grid,
        region=region,
        model=model,
    )
    np.testing.assert_array_equal(tiled.valid[4:8, 4:8], full.valid[4:8, 4:8])
    np.testing.assert_allclose(tiled.data[4:8, 4:8], full.data[4:8, 4:8])


@pytest.mark.parametrize(
    "values,decimals,raw",
    [
        ([0] * 8 + [1], None, 16 / 81),
        ([0, 0, 1] * 3, None, 4 / 9),
        ([0.101, 0.104, 0.106] * 3, 2, 0.01 * 4 / 9),
    ],
)
@pytest.mark.parametrize("normalized", [False, True])
def test_repeated_and_quantized_classes_in_production(
    raster_api, values, decimals, raw, normalized
):
    # Independent population statistics on rounded classes, no fitted production statistics.
    rounded = np.round(values, decimals) if decimals is not None else np.array(values)
    mean = sum(rounded) / 9
    scale = np.sqrt(sum((x - mean) ** 2 for x in rounded) / 9)
    model = (
        RaoNormalization(
            ("b0",), (mean,), (scale,), ((1,),), 1, 6, 9, decimals, "classes"
        )
        if normalized
        else None
    )
    result = rao_q(
        Raster(np.array(values).reshape(3, 3)),
        ["b0"],
        grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
        decimal_places=decimals,
        model=model,
    )
    assert result.valid[1, 1, 0]
    assert result.data[1, 1, 0] == pytest.approx(
        raw / (12 * scale) if normalized else raw
    )


def test_saved_constant_model_reuse_on_changed_values(raster_api):
    # Unchanged JSON schema remains loadable, including pre-existing models.
    model = RaoNormalization.from_statistics(
        ["b0"], [0.29], [[0]], 1000, calibration="constant"
    )
    restored = RaoNormalization.from_json(model.to_json())
    values = np.full((3, 3), 0.29)
    values[1, 1] += 0.01
    applied = restored.apply(Raster(values))
    assert applied.data[1, 1, 0] == pytest.approx(0.01)
    result = rao_q(
        Raster(values),
        ["b0"],
        model=restored,
        grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
    )
    assert result.data[1, 1, 0] == pytest.approx(0.01 * 16 / 81 / 12)
