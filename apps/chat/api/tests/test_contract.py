import json
import re
from pathlib import Path
from typing import Literal, get_args, get_origin

import pytest
from pydantic import TypeAdapter

from app import schemas
from app.models_catalog import MODEL_CATALOG

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "api.md"
TEXT = CONTRACT.read_text(encoding="utf-8")
MARKER = re.compile(r"<!-- (example|enum): ([A-Za-z]\w*) -->")
BLOCK = re.compile(r"<!-- (example|enum): ([A-Za-z]\w*) -->\n```json\n(.*?)\n```", re.S)
BLOCKS = BLOCK.findall(TEXT)
EXAMPLES = [(name, body) for kind, name, body in BLOCKS if kind == "example"]
ENUMS = {name: json.loads(body) for kind, name, body in BLOCKS if kind == "enum"}


def test_every_marker_is_followed_by_json_block():
    assert len(MARKER.findall(TEXT)) == len(BLOCKS)


@pytest.mark.parametrize(("name", "body"), EXAMPLES, ids=[name for name, _ in EXAMPLES])
def test_example_matches_schema(name, body):
    TypeAdapter(getattr(schemas, name)).validate_json(body)


def test_contract_enums_match_schemas():
    literals = {
        name: list(get_args(value))
        for name, value in vars(schemas).items()
        if get_origin(value) is Literal
    }
    assert literals == ENUMS


def test_run_event_union_covers_every_event_type():
    union = get_args(get_args(schemas.RunEvent)[0])
    types = [get_args(model.model_fields["type"].annotation)[0] for model in union]
    assert types == ENUMS["RunEventType"]


def test_contract_has_example_for_every_event_type():
    types = {json.loads(body)["type"] for name, body in EXAMPLES if name == "RunEvent"}
    assert types == set(ENUMS["RunEventType"])


def test_model_catalog_matches_contract_example():
    [body] = [body for name, body in EXAMPLES if name == "ModelCatalog"]
    assert MODEL_CATALOG.model_dump() == json.loads(body)
