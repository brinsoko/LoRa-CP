# app/blueprints/messages/routes.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from flask import Blueprint, render_template, request, url_for, flash

from app.utils.frontend_api import api_json
from app.utils.perms import roles_required

messages_bp = Blueprint("messages", __name__, template_folder="../../templates")


def _build_pagination(meta: Dict[str, Any], dev_id: str | None, per_page: int) -> Dict[str, Any]:
    page = meta.get("page", 1)
    pages = meta.get("pages", 1) or 1

    def _url(target_page: int) -> str:
        return url_for(
            "messages.list_messages",
            page=target_page,
            per_page=per_page,
            dev_id=dev_id or "",
        )

    return {
        "page": page,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
        "prev_url": _url(page - 1) if page > 1 else None,
        "next_url": _url(page + 1) if page < pages else None,
    }


def _decorate_messages(messages: list[dict]) -> list[dict]:
    decorated: list[dict] = []
    for msg in messages:
        ts = msg.get("received_at")
        try:
            dt = datetime.fromisoformat(ts) if ts else None
        except Exception:
            dt = None
        display_ts = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else (ts or "—")
        decorated.append({
            "dev_id": msg.get("dev_id"),
            "payload": msg.get("payload"),
            "rssi": msg.get("rssi"),
            "snr": msg.get("snr"),
            "display_received_at": display_ts,
        })
    return decorated


@messages_bp.route("/", methods=["GET"])
@roles_required("admin")
def list_messages():
    dev_id = (request.args.get("dev_id") or "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)

    params = {"page": page, "per_page": per_page}
    if dev_id:
        params["dev_id"] = dev_id

    resp, payload = api_json("GET", "/api/devices/messages", params=params)
    if resp.status_code != 200:
        flash_msg = payload.get("detail") or payload.get("error") or "Could not load messages."
        flash(flash_msg, "warning")
        messages = []
        pagination = _build_pagination({"page": 1, "pages": 1}, dev_id, per_page)
    else:
        messages = _decorate_messages(payload.get("messages", []))
        pagination = _build_pagination(payload.get("meta", {}), dev_id, per_page)

    return render_template(
        "messages.html",
        messages=messages,
        pagination=pagination,
        selected_dev_id=dev_id,
    )
