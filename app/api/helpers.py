from __future__ import annotations

from flask import jsonify
from werkzeug.exceptions import BadRequest


def json_ok(data=None, status: int = 200):
    payload = {} if data is None else data
    response = jsonify(payload)
    response.status_code = status
    return response


def json_error(key: str, status: int, detail: str | None = None):
    payload = {"error": key, "code": status}
    if detail:
        payload["detail"] = detail
    response = jsonify(payload)
    response.status_code = status
    return response


def parse_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadRequest(description=f"{name} must be an integer.")


def parse_int_list(value, name: str) -> list[int]:
    if value in (None, ""):
        return []

    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise BadRequest(description=f"{name} must be integers.")

    try:
        return [int(part) for part in parts]
    except (TypeError, ValueError):
        raise BadRequest(description=f"{name} must be integers.")


def paginate(query, page, per_page: int = 50):
    page_num = max(1, parse_int(page, "page"))
    per_page_num = max(1, min(500, parse_int(per_page, "per_page")))
    total = query.order_by(None).count()
    items = query.limit(per_page_num).offset((page_num - 1) * per_page_num).all()
    return items, total
