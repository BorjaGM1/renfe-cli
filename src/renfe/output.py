"""JSON rendering for the CLI's output and error payloads (strict JSON: no NaN/Infinity)."""

import json
from typing import Any

from .errors import RenfeError


def render(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)


def render_error(err: RenfeError) -> str:
    return json.dumps({"error": err.to_dict()}, ensure_ascii=False, allow_nan=False)
