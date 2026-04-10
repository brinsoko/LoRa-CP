from __future__ import annotations

from flask import current_app
from flask_restful import Api
from werkzeug.exceptions import HTTPException


class JsonApi(Api):
    def handle_error(self, e):
        status_code = getattr(e, "code", 500)
        error = {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
        }.get(status_code, "internal_server_error" if status_code >= 500 else "error")

        detail = getattr(e, "description", None) or (str(e) if status_code >= 500 else None)
        if status_code >= 500:
            current_app.logger.exception("API error", exc_info=e)

        body = {"error": error, "code": status_code}
        if detail:
            body["detail"] = detail
        return body, status_code
