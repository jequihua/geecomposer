"""Independent numerical oracle and real, credential-free EE graph tests."""

import json
from itertools import product

import ee
import numpy as np
import pytest

from geecomposer.rao import RaoGrid, RaoNormalization, fit_normalization, rao_q


def oracle(values):
    """Full ordered pairs, deliberately different from the graph's unique pairs."""
    x = np.asarray(values, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    return sum(np.linalg.norm(a - b) for a in x for b in x) / len(x) ** 2


def fit(values, **kwargs):
    x = np.asarray(values, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    return RaoNormalization.from_statistics(
        tuple(f"b{i}" for i in range(x.shape[1])),
        x.mean(axis=0),
        np.atleast_2d(np.cov(x, rowvar=False, ddof=0)),
        len(x),
        calibration="synthetic",
        **kwargs,
    )


@pytest.mark.parametrize(
    "values,expected",
    [
        ([0, 1], 0.5),
        ([0, 0, 1], 4 / 9),
        ([0] * 8 + [1], 16 / 81),
        ([[0, 0], [3, 4]], 2.5),
        ([7] * 9, 0),
    ],
)
def test_analytic_oracle(values, expected):
    assert oracle(values) == pytest.approx(expected, abs=1e-10)


def test_gn_scalar_and_frozen_model():
    model = fit([0, 1])
    assert model.means == (0.5,)
    assert model.scales == (0.5,)
    assert model.bound == 12
    assert oracle((np.array([0, 1]) - 0.5) / 0.5) / model.bound == pytest.approx(1 / 12)
    assert RaoNormalization.from_json(model.to_json()) == model
    assert RaoNormalization.from_json(model.to_json()).fingerprint == model.fingerprint
    with pytest.raises(AttributeError):
        model.means = (0,)


def test_pca_original_dimensions_and_population_scale():
    values = np.array([[0, 0, 1], [1, 2, 1], [2, 4, 1], [3, 6, 1]])
    model = fit(values)
    np.testing.assert_allclose(model.scales, [np.sqrt(1.25), np.sqrt(5), 1])
    assert len(model.loadings) == 1
    assert model.retained_fraction == pytest.approx(1)
    assert model.bound == pytest.approx(12 * np.sqrt(3))
    assert fit(np.ones((4, 3))).bound == pytest.approx(12 * np.sqrt(3))


def test_pca_preserves_tied_eigenspace():
    model = fit(list(product([-1, 1], repeat=2)), variance_fraction=0.5)
    assert len(model.loadings) == 2
    assert model.retained_fraction == 1


def test_pca_records_actual_fraction_not_requested_target():
    model = RaoNormalization.from_statistics(
        ["a", "b"],
        [0, 0],
        [[1, 0.5], [0.5, 1]],
        100,
        variance_fraction=0.7,
        calibration="known covariance",
    )
    assert len(model.loadings) == 1
    assert model.retained_fraction == pytest.approx(0.75)
    assert model.bound == pytest.approx(12 * np.sqrt(2))


@pytest.mark.parametrize(
    "change",
    [
        dict(n_sigmas=0),
        dict(n_sigmas=float("nan")),
        dict(sample_count=1),
        dict(scales=(0,)),
        dict(loadings=((2,),)),
        dict(retained_fraction=0),
        dict(decimal_places=True),
    ],
)
def test_invalid_model(change):
    data = json.loads(fit([0, 1]).to_json())
    data.update(change)
    with pytest.raises(ValueError):
        RaoNormalization(**data)


@pytest.mark.parametrize("window,pairs", [(1, 0), (3, 36), (5, 300), (7, 1176)])
def test_real_graph_pair_count(real_ee, window, pairs):
    image = ee.Image.constant([0.0, 1.0]).rename(["b0", "b1"])
    graph = json.loads(
        rao_q(
            image,
            ["b0", "b1"],
            grid=RaoGrid("EPSG:32618", (20, 0, 500000, 0, -20, 1000000)),
            window_size=window,
        ).serialize()
    )
    assert json.dumps(graph).count('"functionName": "Image.sqrt"') == pairs


def test_normalized_graph_and_quantization(real_ee):
    model = fit([0, 1], decimal_places=2)
    image = rao_q(
        ee.Image.constant(1).rename("b0"),
        ["b0"],
        grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
        model=model,
        decimal_places=2,
        missing="valid_subset",
        min_valid=5,
    )
    graph = image.serialize()
    for name in (
        "Image.floor",
        "Image.mod",
        "Image.neighborhoodToArray",
        "Image.reproject",
    ):
        assert name in graph
    assert "rao_gn" in graph
    assert model.fingerprint in graph


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(window_size=True),
        dict(window_size=2),
        dict(window_size=9),
        dict(missing="fill"),
        dict(min_valid=2),
        dict(missing="valid_subset"),
        dict(missing="valid_subset", min_valid=10),
    ],
)
def test_invalid_window_before_graph(kwargs):
    with pytest.raises(ValueError):
        rao_q(None, ["b0"], grid=RaoGrid("EPSG:4326", (1, 0, 0, 0, -1, 0)), **kwargs)


def test_fit_uses_centered_population_covariance(real_ee, monkeypatch):
    answers = iter(
        [{"b0_mean": 0.5, "b0_count": 2, "b0_min": 0, "b0_max": 1}, {"c_0_0": 0.5}]
    )
    calls = []

    def compute(obj):
        calls.append(obj.serialize())
        return next(answers)

    monkeypatch.setattr(ee.data, "computeValue", compute)
    model = fit_normalization(
        ee.Image.constant(1).rename("b0"),
        ["b0"],
        grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
        region=ee.Geometry.Rectangle([0, 0, 1, 1]),
        calibration="fixture",
    )
    assert model.scales == (0.5,)
    assert model.sample_count == 2
    assert "Reducer.unweighted" in calls[0]
    assert "Image.subtract" in calls[1]


def test_fit_rejects_single_sample(real_ee, monkeypatch):
    monkeypatch.setattr(
        ee.data, "computeValue", lambda *_: {"b0_count": 1, "b0_mean": 1}
    )
    with pytest.raises(ValueError, match="two common"):
        fit_normalization(
            ee.Image.constant(1).rename("b0"),
            ["b0"],
            grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
            region=ee.Geometry.Rectangle([0, 0, 1, 1]),
        )


@pytest.mark.parametrize("small_variance", [False, True])
def test_fit_roundoff_constant_and_genuine_small_variance(
    real_ee, monkeypatch, small_variance
):
    from ee_graph import Graph

    # Statistics deliberately contaminated by constant-band summation roundoff.
    delta = 1e-14 if small_variance else 0
    answers = iter(
        [
            {
                "a_count": 1000,
                "a_mean": 0.2900000000000001,
                "a_min": 0.29,
                "a_max": 0.29 + delta,
                "b_mean": 0.5,
                "b_min": 0,
                "b_max": 1,
            },
            {"c_0_0": 1e-26, "c_0_1": 0 if small_variance else 1e-14, "c_1_1": 250},
        ]
    )
    requests = []

    def compute(obj):
        requests.append(Graph(obj))
        return next(answers)

    monkeypatch.setattr(ee.data, "computeValue", compute)
    model = fit_normalization(
        ee.Image.constant([0.29, 0.5]).rename(["a", "b"]),
        ["a", "b"],
        grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
        region=ee.Geometry.Rectangle([0, 0, 1, 1]),
        calibration="roundoff fixture",
    )
    assert list(requests[0].calls("Reducer.minMax"))
    assert model.means[0] == (0.2900000000000001 if small_variance else 0.29)
    assert model.scales[0] == (np.sqrt(1e-29) if small_variance else 1)
    assert model.scales[1] == 0.5
    assert len(model.loadings) == (2 if small_variance else 1)
    assert model.retained_fraction == 1
    if not small_variance:
        assert model.loadings == ((0.0, 1.0),)
    assert RaoNormalization.from_json(model.to_json()) == model


def test_roundoff_fitted_scalar_saved_model_on_changed_values(real_ee, monkeypatch):
    from types import SimpleNamespace
    from rao_raster_double import Raster

    answers = iter(
        [
            {
                "v_count": 1000,
                "v_mean": 0.2900000000000001,
                "v_min": 0.29,
                "v_max": 0.29,
            },
            {"c_0_0": 1e-29},
        ]
    )
    monkeypatch.setattr(ee.data, "computeValue", lambda *_: next(answers))
    model = fit_normalization(
        ee.Image.constant(0.29).rename("v"),
        ["v"],
        grid=RaoGrid("EPSG:32618", (20, 0, 0, 0, -20, 0)),
        region=ee.Geometry.Rectangle([0, 0, 1, 1]),
        calibration="constant",
        decimal_places=2,
    )
    assert model.means == (0.29,)
    assert model.scales == (1.0,)
    assert model.loadings == ((1.0,),)
    restored = RaoNormalization.from_json(model.to_json())
    monkeypatch.setattr(ee, "Image", Raster)
    monkeypatch.setattr(ee, "Reducer", SimpleNamespace(sum=lambda: "sum"))
    assert restored.apply(Raster(0.29, names=["v"])).data.item() == 0
    assert restored.apply(Raster(0.30, names=["v"])).data.item() == pytest.approx(0.01)


def test_final_affine_reprojection_depends_on_neighborhoods(real_ee):
    from ee_graph import Graph

    grid = RaoGrid("EPSG:32618", (20, 0, 12340, 0, -20, 56780))
    graph = Graph(rao_q(ee.Image.constant(1).rename("b0"), ["b0"], grid=grid))
    final = next(graph.calls("Image.reproject"))
    projection = next(graph.calls("Projection", final["crs"]))
    assert graph.constant(projection["transform"]) == list(grid.transform)
    assert graph.constant(projection["crs"]) == grid.crs
    assert len(list(graph.calls("Image.neighborhoodToArray", final["image"]))) == 2
