"""Pagination token helpers for DynamoDB LastEvaluatedKey."""

import json
import base64


def encode_token(last_evaluated_key):
    """Encode a DynamoDB LastEvaluatedKey as a base64 pagination token."""
    if not last_evaluated_key:
        return None
    return base64.b64encode(json.dumps(last_evaluated_key).encode()).decode()


def decode_token(token):
    """Decode a base64 pagination token back to a DynamoDB ExclusiveStartKey."""
    if not token:
        return None
    return json.loads(base64.b64decode(token).decode())
