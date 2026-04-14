"""Custom exceptions for geecomposer."""

from __future__ import annotations


class GeeComposerError(Exception):
    """Base exception for package-specific errors."""


class InvalidAOIError(GeeComposerError):
    """Raised when an AOI input cannot be normalized."""


class InvalidReducerError(GeeComposerError):
    """Raised when a reducer name is unsupported."""


class DatasetNotSupportedError(GeeComposerError):
    """Raised when a dataset preset is unsupported."""


class TransformError(GeeComposerError):
    """Raised when a transform is invalid or returns an unexpected result."""
