"""Typed errors. Each carries the CLI exit code it maps to.

Messages never contain the x-api-key, the bearer token, request bodies or server-supplied free text:
at most an allowlisted Renfe error code (like F_NOT_READABLE) and the HTTP status.
"""

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_BLOCKED = 5
EXIT_SHAPE = 7


class RenfeError(Exception):
    exit_code = 1
    kind = "internal"

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None,
                 transient: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.transient = transient  # what failed, when another try may work (retry.py); not in the output
        self.retries: list[dict] = []  # the command's retried requests, when it failed after them (cli.py)

    def to_dict(self) -> dict:
        out: dict = {"type": self.kind, "message": self.message, "exit_code": self.exit_code}
        if self.status is not None:
            out["http_status"] = self.status
        if self.code:
            out["renfe_code"] = self.code
        if self.retries:
            out["retries"] = self.retries
        return out


class UsageError(RenfeError):
    """Bad input: an unknown station, a malformed date, or a request Renfe rejected as invalid."""

    exit_code = EXIT_USAGE
    kind = "usage"


class AuthError(RenfeError):
    """Renfe refused the anonymous session (401)."""

    exit_code = EXIT_AUTH
    kind = "auth"


class BlockedError(RenfeError):
    """403, 429 or an HTML page instead of JSON: stop and back off, never retried. (What is retried: a 500, 502,
    503 or 504 and a dropped connection, up to 3 tries, see retry.py; and one resend for a 401 to a saved
    session, see client.py.)"""

    exit_code = EXIT_BLOCKED
    kind = "blocked"


class UpstreamError(BlockedError):
    """Renfe 5xx, timeout or dropped connection, after any retries. Shares exit code 5 with blocks and rate
    limits."""

    kind = "upstream_unavailable"


class ApiShapeError(RenfeError):
    """Unexpected status or body shape: Renfe's API probably changed."""

    exit_code = EXIT_SHAPE
    kind = "api_changed"

    def to_dict(self) -> dict:
        out = super().to_dict()
        out["hint"] = "Renfe's web API may have changed; re-capture the request and update renfe/search.py."
        return out
