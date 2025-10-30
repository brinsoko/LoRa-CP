# app/resources/__init__.py
from flask_restful import Api

from .ingest import IngestResource
from .auth import (AuthLogin, AuthLogout, AuthChangePassword, UserList, UserItem, Me,)
from .checkins import CheckinListResource, CheckinItemResource, CheckinExportResource
from .map import MapCheckpoints

def register_resources(api: Api) -> None:
    # REST endpoints
    api.add_resource(IngestResource, "/api/ingest")

    # Auth
    api.add_resource(AuthLogin,          "/auth/login")
    api.add_resource(AuthLogout,         "/auth/logout")
    api.add_resource(AuthChangePassword, "/auth/password")
    api.add_resource(Me,                 "/auth/me")

    # Users (admin)
    api.add_resource(UserList, "/users")
    api.add_resource(UserItem, "/users/<int:user_id>")

    #Checkins
    api.add_resource(CheckinListResource,  "/api/checkins")
    api.add_resource(CheckinItemResource,  "/api/checkins/<int:checkin_id>")
    api.add_resource(CheckinExportResource, "/api/checkins/export.csv")

    # Map
    api.add_resource(MapCheckpoints, "/api/map/checkpoints")