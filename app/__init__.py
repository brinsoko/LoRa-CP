# app/__init__.py — app factory and blueprint registration
from flask import Flask, request
from flask_login import current_user
from .extensions import db, login_manager
from .utils.perms import inject_perms
import logging

def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object("config.Config")

    # ---- Logging setup (after app is created) ----
    # Remove default handlers to avoid duplicate logs when reloading
    for h in list(app.logger.handlers):
        app.logger.removeHandler(h)

    handler = logging.StreamHandler()  # logs to console
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    ))
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.DEBUG)  # app logs at DEBUG

    # Optional: quiet down werkzeug request logs
    logging.getLogger("werkzeug").setLevel(logging.INFO)

    @app.before_request
    def _log_req():
        app.logger.debug(
            "REQ %s %s auth=%s role=%s",
            request.method, request.path,
            getattr(current_user, "is_authenticated", False),
            getattr(current_user, "role", None),
        )

    # ---- Extensions / context ----
    db.init_app(app)
    login_manager.init_app(app)
    app.context_processor(inject_perms)

    # Models must be imported so tables exist
    from . import models  # noqa: F401

    # ---- Blueprints ----
    from .blueprints.auth.routes import auth_bp
    from .blueprints.main.routes import main_bp
    from .blueprints.teams.routes import teams_bp
    from .blueprints.checkpoints.routes import checkpoints_bp
    from .blueprints.checkins.routes import checkins_bp
    from .blueprints.rfid.routes import rfid_bp
    from .blueprints.map.routes import maps_bp
    from .blueprints.groups.routes import groups_bp

    app.register_blueprint(groups_bp, url_prefix="/groups")
    app.register_blueprint(maps_bp, url_prefix="/map")
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(teams_bp, url_prefix="/teams")
    app.register_blueprint(checkpoints_bp, url_prefix="/checkpoints")
    app.register_blueprint(checkins_bp, url_prefix="/checkins")
    app.register_blueprint(rfid_bp, url_prefix="/rfid")

    with app.app_context():
        db.create_all()

    with app.app_context():
        from pprint import pprint
        print("\n=== URL MAP ===")
        pprint(sorted([(r.endpoint, list(r.methods), str(r)) for r in app.url_map.iter_rules()]))
        print("===============\n")

    return app