"""Database-specific native MongoDB designs for BIRD mini-dev."""

from __future__ import annotations

from .registry import NATIVE_DESIGN_MODULES, get_native_design

__all__ = ["NATIVE_DESIGN_MODULES", "get_native_design"]
