# app/utils/frontend_api.py
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

from flask import current_app, request, make_response


def _server_name() -> str:
    if current_app.config.get("SERVER_NAME"):
        return current_app.config["SERVER_NAME"].split(":")[0]
    host = request.host.split(":")[0] if request.host else "localhost"
    return host or "localhost"


def api_request(method: str,
                path: str,
                *,
                params: Optional[Dict[str, Any]] = None,
                json: Optional[Dict[str, Any]] = None,
                data: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None):
    """Call an internal API endpoint using the current session cookie."""
    if not path.startswith("/"):
        path = "/" + path

    with current_app.test_client() as client:
        server_name = _server_name()
        for name, value in request.cookies.items():
            client.set_cookie(name, value, domain=server_name, path="/")

        response = client.open(
            path,
            method=method.upper(),
            query_string=params,
            json=json,
            data=data,
            headers=headers,
            follow_redirects=False,
        )

        return response


def transfer_api_cookies(api_response, flask_response):
    """Copy Set-Cookie headers from an API response onto the outgoing response."""
    for header in api_response.headers.getlist("Set-Cookie"):
        flask_response.headers.add("Set-Cookie", header)
    return flask_response


def api_json(method: str,
             path: str,
             *,
             params: Optional[Dict[str, Any]] = None,
             json: Optional[Dict[str, Any]] = None,
             data: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None) -> Tuple[Any, Dict[str, Any]]:
    resp = api_request(method, path, params=params, json=json, data=data, headers=headers)
    payload = resp.get_json(silent=True) or {}
    return resp, payload
