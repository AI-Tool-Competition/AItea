import json
from datetime import datetime, timezone
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)
