# app/__init__.py
from flask import Flask, request, current_app, render_template, session, g
from flask_babel import get_locale
from flask_login import current_user
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from .extensions import db, login_manager, babel, limiter
from sqlalchemy import inspect, text
from .api.auth import auth_api_bp
from .api.checkpoints import checkpoints_api_bp
from .api.groups import groups_api_bp
from .api.teams import teams_api_bp
from .api.helpers import json_error
from .resources.checkins import checkins_api_bp
from .resources.docs_resource import docs_api_bp
from .resources.ingest import ingest_api_bp
from .resources.lora import lora_devices_api_bp
from .resources.map import map_api_bp
from .resources.messages import messages_api_bp
from .resources.rfid import rfid_api_bp
from .resources.score_rules import score_rules_api_bp
from .resources.scores import scores_api_bp
from .api.transfer import transfer_api_bp
from app.utils.time import to_datetime_local
from .utils.perms import inject_perms
from .utils.csrf import protect_request, get_csrf_token, csrf_input
from .utils.competition import (
    ensure_default_competition,
    get_current_competition,
    get_current_competition_role,
    get_user_competitions,
)
import logging
import os
import tempfile

def create_app(config_overrides: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object("config.Config")
    if config_overrides:
        app.config.update(config_overrides)
    app.jinja_env.filters["local_dt"] = to_datetime_local

    # Trust X-Forwarded-* headers from a single reverse proxy hop (Caddy in
    # prod). Without this, OAuth redirect_uri uses the internal scheme/host
    # (`http://web:5000/...`) which Google rejects as a redirect mismatch.
    # Safe in dev too: it's a no-op when no proxy headers are present.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    os.makedirs(app.instance_path, exist_ok=True)

    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        if app.config.get("TESTING"):
            test_db_dir = tempfile.mkdtemp(prefix="lora-kt-test-db-")
            db_path = os.path.join(test_db_dir, "app.db")
            app.config["_EPHEMERAL_TEST_DB"] = True
        else:
            db_path = os.path.join(app.instance_path, "app.db")
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"

    app.register_blueprint(auth_api_bp)
    app.register_blueprint(checkpoints_api_bp)
    app.register_blueprint(groups_api_bp)
    app.register_blueprint(teams_api_bp)
    app.register_blueprint(checkins_api_bp)
    app.register_blueprint(docs_api_bp)
    app.register_blueprint(ingest_api_bp)
    app.register_blueprint(lora_devices_api_bp)
    app.register_blueprint(map_api_bp)
    app.register_blueprint(messages_api_bp)
    app.register_blueprint(rfid_api_bp)
    app.register_blueprint(score_rules_api_bp)
    app.register_blueprint(scores_api_bp)
    app.register_blueprint(transfer_api_bp)

    # logging — DEBUG only when the app is in debug/testing mode, INFO otherwise.
    # Production logs every request at INFO via werkzeug; the per-request DEBUG
    # line below would otherwise log header/role info on every hit.
    log_level = logging.DEBUG if (app.debug or app.config.get("TESTING")) else logging.INFO
    for h in list(app.logger.handlers):
        app.logger.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setLevel(log_level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    app.logger.addHandler(handler)
    app.logger.setLevel(log_level)
    logging.getLogger("werkzeug").setLevel(logging.INFO)

    if log_level == logging.DEBUG:
        @app.before_request
        def _log_req():
            app.logger.debug(
                "REQ %s %s endpoint=%s auth=%s role=%s ua=%s",
                request.method, request.path, request.endpoint,
                getattr(current_user, "is_authenticated", False),
                getattr(current_user, "role", None),
                request.headers.get("User-Agent", "")[:80],
            )

    @app.before_request
    def _csrf_protect():
        protect_request()

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"

    # Wire the rate limiter. It's a no-op until a route uses @limiter.limit.
    # Disabled in tests so existing assertions don't trip on 429.
    if app.config.get("TESTING") and not app.config.get("RATELIMIT_ENABLED"):
        app.config["RATELIMIT_ENABLED"] = False
    limiter.init_app(app)

    def _select_locale() -> str:
        lang = session.get("lang")
        if lang and lang in app.config.get("LANGUAGES", {}):
            return lang
        return request.accept_languages.best_match(app.config.get("LANGUAGES", {}).keys()) or app.config.get("BABEL_DEFAULT_LOCALE", "en")

    babel.init_app(app, locale_selector=_select_locale)

    app.context_processor(inject_perms)

    # Ensure models are imported
    from . import models  # noqa: F401

    # ---- Blueprints (HTML) ----
    from .blueprints.auth.routes import auth_bp
    from .blueprints.main.routes import main_bp
    from .blueprints.teams.routes import teams_bp
    from .blueprints.checkpoints.routes import checkpoints_bp
    from .blueprints.checkins.routes import checkins_bp
    from .blueprints.rfid.routes import rfid_bp
    from .blueprints.map.routes import maps_bp
    from .blueprints.groups.routes import groups_bp
    from .blueprints.lora.routes import lora_bp
    from app.blueprints.messages.routes import messages_bp
    from app.blueprints.docs.routes import docs_bp
    from app.blueprints.users.routes import users_bp
    from app.blueprints.judges.routes import judges_bp
    from app.blueprints.scores.routes import scores_bp
    from app.blueprints.sheets.routes import sheets_bp
    from app.blueprints.audit.routes import audit_bp
    from app.blueprints.firmware.routes import firmware_bp

    app.register_blueprint(users_bp, url_prefix="/users")
    app.register_blueprint(judges_bp, url_prefix="/judges")
    app.register_blueprint(scores_bp, url_prefix="/scores")
    app.register_blueprint(audit_bp, url_prefix="/audit")
    app.register_blueprint(docs_bp, url_prefix="/docs")
    app.register_blueprint(messages_bp, url_prefix="/messages")
    app.register_blueprint(lora_bp, url_prefix="/lora")
    app.register_blueprint(groups_bp, url_prefix="/groups")
    app.register_blueprint(maps_bp,   url_prefix="/map")
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(teams_bp,        url_prefix="/teams")
    app.register_blueprint(checkpoints_bp,  url_prefix="/checkpoints")
    app.register_blueprint(checkins_bp,     url_prefix="/checkins")
    app.register_blueprint(rfid_bp,         url_prefix="/rfid")
    app.register_blueprint(sheets_bp,       url_prefix="/sheets")
    app.register_blueprint(firmware_bp,     url_prefix="/firmware")

    with app.app_context():
        db.create_all()
        try:
            ensure_default_competition()
        except Exception:
            app.logger.exception("Failed to ensure default competition")
        try:
            insp = inspect(db.engine)
            cols = {c["name"] for c in insp.get_columns("competitions")}
            if "ingest_password_hash" not in cols:
                with db.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE competitions ADD COLUMN ingest_password_hash VARCHAR(255)"))
        except Exception:
            app.logger.exception("Failed to ensure competitions.ingest_password_hash column")
        try:
            insp = inspect(db.engine)
            cols = {c["name"] for c in insp.get_columns("users")}
            if "last_competition_id" not in cols:
                with db.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN last_competition_id INTEGER"))
        except Exception:
            app.logger.exception("Failed to ensure users.last_competition_id column")
        try:
            insp = inspect(db.engine)
            cols = {c["name"] for c in insp.get_columns("checkins")}
            with db.engine.begin() as conn:
                if "created_by_user_id" not in cols:
                    conn.execute(text("ALTER TABLE checkins ADD COLUMN created_by_user_id INTEGER"))
                if "created_by_device_id" not in cols:
                    conn.execute(text("ALTER TABLE checkins ADD COLUMN created_by_device_id INTEGER"))
        except Exception:
            app.logger.exception("Failed to ensure checkins audit columns")
        try:
            insp = inspect(db.engine)
            cols = {c["name"] for c in insp.get_columns("competitions")}
            if "hide_gps_map" not in cols:
                with db.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE competitions ADD COLUMN hide_gps_map BOOLEAN NOT NULL DEFAULT 0"))
        except Exception:
            app.logger.exception("Failed to ensure competitions.hide_gps_map column")
        try:
            insp = inspect(db.engine)
            cols = {c["name"] for c in insp.get_columns("firmware_files")}
            if cols and "nvs_size" not in cols:
                with db.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE firmware_files ADD COLUMN nvs_size INTEGER DEFAULT 20480"))
                    conn.execute(text("UPDATE firmware_files SET nvs_size = 20480 WHERE nvs_size IS NULL"))
        except Exception:
            app.logger.exception("Failed to ensure firmware_files.nvs_size column")

    def _http_detail(e: Exception, default: str) -> str:
        if isinstance(e, HTTPException):
            return getattr(e, "description", None) or default
        return default

    @app.errorhandler(400)
    def bad_request(e):
        return json_error("bad_request", 400, _http_detail(e, "Bad request."))

    @app.errorhandler(401)
    def unauthorized(e):
        return json_error("unauthorized", 401, _http_detail(e, "Unauthorized."))

    @app.errorhandler(403)
    def forbidden(e):
        app.logger.warning(
            "403 Forbidden at %s (endpoint=%s) auth=%s role=%s",
            request.path, request.endpoint,
            getattr(current_user, "is_authenticated", False),
            getattr(current_user, "role", None),
        )
        return json_error("forbidden", 403, _http_detail(e, "Forbidden."))

    @app.errorhandler(404)
    def not_found(e):
        return json_error("not_found", 404, _http_detail(e, "Not found."))

    @app.errorhandler(405)
    def method_not_allowed(e):
        return json_error("method_not_allowed", 405, _http_detail(e, "Method not allowed."))

    @app.errorhandler(409)
    def conflict(e):
        return json_error("conflict", 409, _http_detail(e, "Conflict."))

    @app.errorhandler(422)
    def unprocessable_entity(e):
        return json_error("error", 422, _http_detail(e, "Unprocessable entity."))

    @app.errorhandler(500)
    def internal_server_error(e):
        current_app.logger.exception("500 Internal Server Error at %s", request.path)
        return json_error("internal_server_error", 500, "Internal server error.")

    @app.get("/health")
    def health():
        # Cheap liveness probe — if the process responds, it's up.
        return {"ok": True}, 200

    @app.get("/ready")
    def ready():
        # Readiness probe — also exercises the DB so a locked or missing
        # SQLite file shows up as 503 instead of pretending to be healthy.
        try:
            db.session.execute(text("SELECT 1"))
            return {"ok": True}, 200
        except Exception as exc:
            current_app.logger.exception("readiness probe failed")
            return {"ok": False, "error": exc.__class__.__name__}, 503

    @app.get("/api")
    def api_root():
        return {
            "service": "LoRa KT API",
            "version": current_app.config.get("APP_VERSION", "v1"),
            "docs": "/api/docs/openapi.json",
        }, 200

    @app.before_request
    def _set_locale_on_g():
        # Babel determines the locale via locale_selector; store it for templates
        g.locale = str(get_locale() or _select_locale())

    @app.before_request
    def _set_competition_on_g():
        g.current_competition = get_current_competition()

    @app.context_processor
    def inject_current_app():
        return dict(
            current_app=current_app,
            csrf_token=get_csrf_token,
            csrf_input=csrf_input,
            languages=current_app.config.get("LANGUAGES", {}),
            current_locale=getattr(g, "locale", None) or str(get_locale() or _select_locale()),
            current_competition=getattr(g, "current_competition", None),
            current_competition_role=get_current_competition_role(),
            available_competitions=(
                get_user_competitions(current_user.id)
                if getattr(current_user, "is_authenticated", False)
                else []
            ),
        )

    return app
