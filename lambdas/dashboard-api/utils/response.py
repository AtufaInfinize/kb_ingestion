"""FastAPI response helper — JSON serialization with Decimal/datetime support."""

import json
from fastapi.responses import JSONResponse


class DecimalJSONResponse(JSONResponse):
    """JSONResponse that serializes Decimal and datetime objects via str()."""

    def render(self, content) -> bytes:
        return json.dumps(content, default=str).encode("utf-8")


def api_response(status_code: int, body: dict) -> DecimalJSONResponse:
    """Return a DecimalJSONResponse with the given status code and body."""
    return DecimalJSONResponse(content=body, status_code=status_code)
