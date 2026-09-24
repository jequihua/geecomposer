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
