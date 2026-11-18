"""Loader/validator for the frozen JSON schemas in ``scopt/formats/schemas/``.

Conventions shared by every schema (frozen by WP0):

* ids and content hashes are ``"sha256:<64 lowercase hex>"``;
* addresses, flags and guest/host values are lowercase ``"0x…"`` strings;
  sizes, counts, indices and offsets relative to a unit are JSON integers;
* UnitIds are ``"tb:<32 hex>#<k>"``;
* keys matching ``^x_`` are allowed extensions (they are hashed into ids like
  any other key); every other unknown key is rejected.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_NAMES = ("cache_region.v1", "profile.v1", "execution_bundle.v1", "artifact_sidecar.v1",
                "envlayout.v1")


class SchemaValidationError(ValueError):
    def __init__(self, schema: str, errors: list[str]):
        self.schema = schema
        self.errors = errors
        super().__init__(f"{schema}: " + "; ".join(errors[:5])
                         + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""))


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    if name not in SCHEMA_NAMES:
        raise KeyError(f"unknown schema {name!r}; known: {SCHEMA_NAMES}")
    text = resources.files("scopt.formats").joinpath("schemas", f"{name}.json").read_text("utf-8")
    return json.loads(text)


@lru_cache(maxsize=None)
def validator(name: str) -> Draft202012Validator:
    schema = load_schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def errors(instance: Any, name: str) -> list[str]:
    return sorted(
        f"{'/'.join(str(p) for p in e.absolute_path) or '$'}: {e.message}"
        for e in validator(name).iter_errors(instance))


def validate(instance: Any, name: str) -> None:
    errs = errors(instance, name)
    if errs:
        raise SchemaValidationError(name, errs)
