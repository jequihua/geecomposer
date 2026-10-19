"""Sanity checks for the current package surface."""

from geecomposer import (
    compose,
    compose_yearly,
    export_to_drive,
    export_to_gcs,
    initialize,
)


def test_public_api_symbols_are_importable() -> None:
    assert callable(initialize)
    assert callable(compose)
    assert callable(compose_yearly)
    assert callable(export_to_drive)
    assert callable(export_to_gcs)


def test_coarse_module_is_importable_without_changing_existing_exports() -> None:
    import geecomposer
    from geecomposer import coarse
    from geecomposer.grouping import monthly_periods, validate_periods
    from geecomposer.rao import rao_q

    assert callable(geecomposer.rao) and geecomposer.rao.__module__ == "geecomposer.rao"  # still the function
    assert geecomposer.rao_q is rao_q and callable(monthly_periods) and callable(validate_periods)
    assert coarse.PRESETS == (100, 250, 500, 1000, 5000, 10000)
    for name in ("grid", "feature_names", "cell_region", "composite", "rao", "composite_periods",
                 "fine_first_reference", "reference_rao", "build_snapshot", "admit"):
        assert callable(getattr(coarse, name))
    assert "coarse" not in geecomposer.__all__
