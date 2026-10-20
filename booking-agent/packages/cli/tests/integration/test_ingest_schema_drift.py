"""The support ingest's generated schema files must match the CLI schemas.

The ingest lives outside this repository. Set ``USP_SPEC_INGEST_DIR`` to its
``apps-script`` folder to run this check; it is skipped otherwise.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_INGEST_DIR = os.environ.get("USP_SPEC_INGEST_DIR")
_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "generate_ingest_schemas.py"

pytestmark = pytest.mark.skipif(not _INGEST_DIR, reason="USP_SPEC_INGEST_DIR is not set")


def _rendered():
    spec = importlib.util.spec_from_file_location("generate_ingest_schemas", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.rendered()


@pytest.mark.parametrize("filename", ["Schema.gs", "FaultSchema.gs"])
def test_ingest_schema_file_matches_cli_schema(filename):
    path = Path(_INGEST_DIR) / filename
    assert path.read_text(encoding="utf-8") == _rendered()[filename], (
        f"{path} drifted; regenerate it with scripts/generate_ingest_schemas.py"
    )
