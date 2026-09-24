"""Metadata payload helpers.

Builds the ``geecomposer:*`` property dict attached to composed images.
"""

from __future__ import annotations


def build_metadata_payload(
    dataset: str | None,
    collection: str | None,
    start: str | None,
    end: str | None,
    reducer: str,
    transform_name: str | None,
    metadata: dict | None = None,
    *,
    preprocess_name: str | None = None,
    mask: str | None = None,
    filters: dict | None = None,
    select: str | list[str] | None = None,
) -> dict:
    """Build a metadata property dict for a composed ``ee.Image``.

    Returns a flat dict of ``geecomposer:*`` prefixed keys suitable for
    passing to ``ee.Image.set()``.
    """
    props: dict = {
        "geecomposer:dataset": dataset or "",
        "geecomposer:collection": collection or "",
        "geecomposer:start": start or "",
        "geecomposer:end": end or "",
        "geecomposer:reducer": reducer,
        "geecomposer:transform": transform_name or "",
    }
    props.update(
        {
            "geecomposer:preprocess": preprocess_name or "",
            "geecomposer:mask": mask or "",
            "geecomposer:filters": dict(filters or {}),
            "geecomposer:select": (
                [select] if isinstance(select, str) else list(select or [])
            ),
        }
    )
    if mask == "s2_cloud_score_plus":
        from ..datasets import sentinel2

        props["geecomposer:mask_band"] = sentinel2._CS_PLUS_BAND
        props["geecomposer:mask_threshold"] = sentinel2._CS_PLUS_THRESHOLD
    if metadata:
        for key, value in metadata.items():
            props[f"geecomposer:user:{key}"] = value
    return props
