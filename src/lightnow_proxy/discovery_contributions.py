"""A collection boundary: project native resolution facts, never runtime values.

The API's existing catalog resolvers know which declared inputs were bound.
Only their bounded structural descriptor enters this module; commands, argv,
environment/header values, URLs, file contents and tool metadata do not.
"""
from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

from lightnow_proxy.capture import is_sensitive_key, json_size

INPUT_ID = r"^(env|header|package_argument|runtime_argument|remote_variable):[0-9]{1,3}$"


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    schema_version: StrictInt = Field(ge=1, le=1)
    server_version_id: UUID = Field(strict=False)
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    variant_kind: Literal["package", "remote"]
    variant_index: StrictInt = Field(ge=0, le=99)


class _Template(_Target):
    variant: dict[str, Any]
    public_choice_input_ids: list[str] = Field(default_factory=list, max_length=80)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    input_id: str = Field(pattern=INPUT_ID)
    provided: StrictBool
    file_binding: StrictBool = False
    public_choice_index: StrictInt | None = Field(default=None, ge=0, le=99)


class _Resolved(_Target):
    inputs: list[_Input] = Field(max_length=80)
    omitted_unknown_fields: StrictBool


def _declarations(template: _Template) -> dict[str, dict[str, Any]]:
    fields = {"env": "environmentVariables", "package_argument": "packageArguments", "runtime_argument": "runtimeArguments"}
    if template.variant_kind == "remote":
        fields = {"header": "headers", "remote_variable": "variables"}
    declarations = {}
    for scope, field in fields.items():
        values = template.variant.get(field, {} if scope == "remote_variable" else [])
        if scope == "remote_variable":
            if not isinstance(values, dict):
                raise ValueError("Invalid public declarations")
            values = [{**values[key], "name": key} if isinstance(values[key], dict) else values[key] for key in sorted(values)]
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            raise ValueError("Invalid public declarations")
        declarations.update((f"{scope}:{index}", value) for index, value in enumerate(values))
    if len(declarations) > 80:
        raise ValueError("Too many public declarations")
    return declarations


def _kind(definition: dict[str, Any]) -> str:
    names = [definition.get("name", ""), definition.get("valueHint", "")]
    variables = definition.get("variables", {})
    if not all(isinstance(name, str) for name in names) or not isinstance(variables, dict):
        raise ValueError("Invalid public input")
    if definition.get("isSecret", False) is not False or any(is_sensitive_key(name) for name in names):
        return "secret"
    if any(not isinstance(value, dict) or value.get("isSecret", False) is not False for value in variables.values()):
        return "secret"
    words = set(re.split(r"[^a-z0-9]+", " ".join(names).lower()))
    if definition.get("format") == "filepath" or words & {"path", "directory", "directories", "file"}:
        return "path"
    if words & {"url", "uri", "endpoint"}:
        return "url"
    return {"number": "number", "boolean": "boolean"}.get(definition.get("format"), "string")


def sanitized_setup_preview(template: Any, resolved_inputs: Any) -> dict[str, Any] | None:
    """Return only the public preview schema, or explicitly unshareable.

    Public identity is cross-checked, not inferred from a command or alias. The
    binding descriptor is delivered with the current managed context; the
    caller still enforces its ordinary actor/client/assignment authority.
    Validation errors are deliberately not rendered or logged.
    """
    try:
        if json_size(template) > 16_384 or json_size(resolved_inputs) > 16_384:
            return None
        public = _Template.model_validate(template)
        bound = _Resolved.model_validate(resolved_inputs)
        coordinates = ("server_version_id", "snapshot_sha256", "variant_kind", "variant_index")
        if any(getattr(public, field) != getattr(bound, field) for field in coordinates):
            return None
        declarations = _declarations(public)
        ids = [item.input_id for item in bound.inputs]
        if len(ids) != len(set(ids)) or set(ids) != declarations.keys():
            return None
        allowed_choices = set(public.public_choice_input_ids)
        if len(allowed_choices) != len(public.public_choice_input_ids) or not allowed_choices <= declarations.keys():
            return None
        inputs = []
        omitted = bound.omitted_unknown_fields
        for item in bound.inputs:
            definition = declarations[item.input_id]
            kind = _kind(definition)
            if item.file_binding:
                if not item.provided or kind not in {"string", "path", "secret"}:
                    return None
                if kind != "secret":
                    kind = "file"
            entry = {"input_id": item.input_id, "provided": item.provided, "value_kind": kind}
            choice = item.public_choice_index
            choices = definition.get("choices", [])
            if choice is not None:
                if (item.provided and item.input_id in allowed_choices and kind in {"string", "number", "boolean"}
                        and isinstance(choices, list) and all(isinstance(value, str) for value in choices)
                        and choice < len(choices)):
                    entry["public_choice_index"] = choice
                else:
                    omitted = True
            inputs.append(entry)
        return {"schema_version": 1, "variant_index": public.variant_index, "inputs": inputs,
                "omitted_unknown_fields": omitted}
    except (ValidationError, ValueError, TypeError, OverflowError):
        return None
