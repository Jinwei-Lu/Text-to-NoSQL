from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest


def test_runtime_and_formatter_are_the_only_logging_implementation_modules() -> None:
    formatter_module = importlib.import_module("tend.observability._formatters")
    runtime_module = importlib.import_module("tend.observability._runtime")
    public_module = importlib.import_module("tend.observability")

    assert public_module.RunLogger is runtime_module.RunLogger
    assert public_module.setup_logging is runtime_module.setup_logging
    assert public_module.new_run_id is runtime_module.new_run_id
    assert hasattr(formatter_module, "render_llm_transcript_markdown")

    formatter_source = inspect.getsource(formatter_module)
    assert "class RunLogger" not in formatter_source
    assert "class _JsonlSink" not in formatter_source
    assert not (Path(__file__).parents[1] / "src/tend/observability/logging.py").exists()


def test_old_logging_facade_module_is_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("tend.observability.logging")
