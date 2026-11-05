"""Bounded diagnostic artifacts; invalid-schema values never enter reports."""

from typing import Literal

from pydantic import Field

from .bundle_evidence import Strict

MAX_CANDIDATE_BYTES = 16 * 1024
MAX_ISSUES = 24


class SchemaIssue(Strict):
    path: list[str | int] = Field(max_length=16)
    error_type: str


class RejectedCandidate(Strict):
    status: Literal["schema_invalid", "stored", "too_large"]
    action: dict | None = None
    serialized_bytes: int | None = None


def schema_issues(exc, adapter):
    # Only names explicitly declared in this action schema may appear in a path.
    names = {"tool", "finish"}

    def visit(node):
        if isinstance(node, dict):
            names.update(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(adapter.json_schema())
    issues = []
    errors = exc.errors(include_input=False, include_context=False, include_url=False)
    for error in errors[:MAX_ISSUES]:
        path = [part if (type(part) is int and 0 <= part <= 10000) or
                (type(part) is str and part in names) else "<unknown>"
                for part in error["loc"][:16]]
        issues.append(SchemaIssue(path=path, error_type=error["type"]))
    truncated = len(errors) > MAX_ISSUES or any(len(e["loc"]) > 16 for e in errors[:MAX_ISSUES])
    return issues, truncated


def rejected_candidate(action):
    """Only called AFTER action schema validation; never truncate JSON into a fake action."""
    size = len(action.model_dump_json().encode("utf-8"))
    if size > MAX_CANDIDATE_BYTES:
        return RejectedCandidate(status="too_large", serialized_bytes=size)
    return RejectedCandidate(status="stored", serialized_bytes=size, action=action.model_dump(mode="json"))
