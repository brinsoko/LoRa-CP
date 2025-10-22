# app/blueprints/messages/routes.py
from __future__ import annotations
from flask import Blueprint, render_template, request
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import LoRaMessage  # make sure this model exists
from app.utils.perms import roles_required

messages_bp = Blueprint("messages", __name__, template_folder="../../templates")

@messages_bp.route("/", methods=["GET"])
@roles_required("admin")  # <-- admin-only gate
def list_messages():
    # Simple filters (optional)
    dev_id = request.args.get("dev_id", type=int)
    q = LoRaMessage.query
    if dev_id is not None:
        q = q.filter(LoRaMessage.dev_id == dev_id)

    q = q.order_by(LoRaMessage.received_at.desc())

    # Optional: very light pagination
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 200)
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)

    return render_template(
        "messages.html",
        messages=pagination.items,
        pagination=pagination,
        selected_dev_id=dev_id if dev_id is not None else "",
    )