"""Path-name utilities shared across exporter, evaluator, and validation.

Every function here is pure and has no I/O side effects so it is safe to
import from any layer.  See ``docs`` and ``Proposals/04`` for the layout
contract these helpers protect.
"""

from __future__ import annotations

import re

__all__ = ["safe_dirname"]


_SAFE_DIRNAME_RE = re.compile(r"[^\w\-.]+")


def safe_dirname(name: str) -> str:
    """Return a filesystem-safe directory name derived from ``name``.

    Replaces every run of characters outside ``[A-Za-z0-9_.-]`` with a
    single underscore, trims leading/trailing underscores, and falls
    back to a deterministic placeholder when the result would be empty.

    The original ``name`` remains the canonical identifier inside YAML
    payloads (e.g. ``test_suite.yaml.report_id``); only the on-disk
    directory uses the sanitised form.  Producers and consumers must
    therefore agree on this single helper to round-trip the mapping.
    """
    if not name:
        return "_"
    cleaned = _SAFE_DIRNAME_RE.sub("_", name.strip())
    cleaned = cleaned.strip("_")
    return cleaned or "_"
