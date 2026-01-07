# app/__init__.py
from flask import Flask, request, current_app, render_template, session, g
from flask_babel import get_locale
from flask_restful import Api
from flask_login import current_user
from .extensions import db, login_manager, babel
from .resources import register_resources
from app.utils.time import to_datetime_local
from .utils.perms import inject_perms
from .utils.competition import (
    ensure_default_competition,
    get_current_competition,
    get_current_competition_role,
    get_user_competitions,
)
import logging
import os

def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object("config.Config")
    app.jinja_env.filters["local_dt"] = to_datetime_local

    os.makedirs(app.instance_path, exist_ok=True)

    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        db_path = os.path.join(app.instance_path, "app.db")
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"

    # REST API
    api = Api(app)
    register_resources(api)

    # logging …
    for h in list(app.logger.handlers):
        app.logger.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.DEBUG)
    logging.getLogger("werkzeug").setLevel(logging.INFO)

    @app.before_request
    def _log_req():
        app.logger.debug(
            "REQ %s %s endpoint=%s auth=%s role=%s ua=%s",
            request.method, request.path, request.endpoint,
            getattr(current_user, "is_authenticated", False),
            getattr(current_user, "role", None),
            request.headers.get("User-Agent", "")[:80],
        )

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"

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

    app.register_blueprint(users_bp, url_prefix="/users")
    app.register_blueprint(judges_bp, url_prefix="/judges")
    app.register_blueprint(scores_bp, url_prefix="/scores")
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

    with app.app_context():
        db.create_all()
        try:
            ensure_default_competition()
        except Exception:
            app.logger.exception("Failed to ensure default competition")

    @app.errorhandler(403)
    def forbidden(e):
        app.logger.warning(
            "403 Forbidden at %s (endpoint=%s) auth=%s role=%s",
            request.path, request.endpoint,
            getattr(current_user, "is_authenticated", False),
            getattr(current_user, "role", None),
        )
        if request.path.startswith("/api") or request.accept_mimetypes.best == "application/json":
            return {"error": "forbidden"}, 403
        return render_template("403.html"), 403

    @app.get("/health")
    def health():
        return {"ok": True}, 200

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
