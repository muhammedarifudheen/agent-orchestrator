"""Helpers for the pipeline tests: synthetic draft specs built with the shared ingestor helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ingest.common import make_operation, make_param, make_spec, ok_example, invalid_argument_example
from pipeline import Layout


def layout_in(tmp_path: Path) -> Layout:
    """A throwaway project layout (inbox/specs/servers/tests all under tmp_path)."""
    for sub in ("inbox", "specs", "servers", "tests"):
        (tmp_path / sub).mkdir()
    return Layout(root=tmp_path)


def read_op(name: str = "get_item", confidence: float = 0.9, method: str = "GET", path: str = "/items/{item_id}",
            reasons: list[str] | None = None) -> dict[str, Any]:
    return make_operation(
        name=name, summary=f"Fetch one {name} record by id.", when_to_call="Call when you know the record id.",
        method=method, path=path,
        params=[make_param("item_id", "integer", "path", True, "The record id.", constraints={"minimum": 1})],
        response_type="object", response_description="The record with id and label fields.",
        examples=[ok_example({"item_id": 1}, {"id": 1, "label": "x"}, ["id"]),
                  invalid_argument_example({"item_id": 0})],
        source="openapi", confidence=confidence, reasons=reasons)


def spec_with(system: str, *ops: dict[str, Any]) -> dict[str, Any]:
    return make_spec(system_name=system, base_url="https://api.example.test", description=f"{system} test API.",
                     operations=list(ops))


def drop(inbox: Path, spec: dict[str, Any], filename: str | None = None) -> Path:
    path = inbox / (filename or f"{spec['system_name']}.yaml")
    path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path
