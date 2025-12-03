# app/resources/__init__.py
from flask_restful import Api

from .ingest import IngestResource
from .auth import (
    AuthLogin,
    AuthLogout,
    AuthChangePassword,
    UserList,
    UserItem,
    Me,
)
from .checkins import CheckinListResource, CheckinItemResource, CheckinExportResource
from .teams import TeamListResource, TeamItemResource, TeamActiveGroupResource
from .groups import GroupListResource, GroupItemResource
from .checkpoints_rest import (
    CheckpointListResource,
    CheckpointItemResource,
    CheckpointImportResource,
)
from .rfid import (
    RFIDCardListResource,
    RFIDCardItemResource,
    RFIDScanResource,
    RFIDBulkImportResource,
    RFIDVerifyResource,
)
from .lora import LoRaDeviceListResource, LoRaDeviceItemResource
from .messages import LoRaMessageListResource
from .map import MapCheckpoints, LoRaMapPoints
from .docs_resource import ApiDocsListResource, ApiSpecResource


def register_resources(api: Api) -> None:
    # System / health
    api.add_resource(IngestResource, "/api/ingest")

    # Auth
    api.add_resource(AuthLogin,          "/api/auth/login")
    api.add_resource(AuthLogout,         "/api/auth/logout")
    api.add_resource(AuthChangePassword, "/api/auth/password")
    api.add_resource(Me,                 "/api/auth/me")

    # Users (admin)
    api.add_resource(UserList, "/api/users")
    api.add_resource(UserItem, "/api/users/<int:user_id>")

    # Teams
    api.add_resource(TeamListResource, "/api/teams")
    api.add_resource(TeamItemResource, "/api/teams/<int:team_id>")
    api.add_resource(TeamActiveGroupResource, "/api/teams/<int:team_id>/active-group")

    # Groups & Checkpoints
    api.add_resource(GroupListResource, "/api/groups")
    api.add_resource(GroupItemResource, "/api/groups/<int:group_id>")

    api.add_resource(CheckpointListResource, "/api/checkpoints")
    api.add_resource(CheckpointItemResource, "/api/checkpoints/<int:checkpoint_id>")
    api.add_resource(CheckpointImportResource, "/api/checkpoints/import")

    # RFID
    api.add_resource(RFIDCardListResource, "/api/rfid/cards")
    api.add_resource(RFIDCardItemResource, "/api/rfid/cards/<int:card_id>")
    api.add_resource(RFIDScanResource, "/api/rfid/scan")
    api.add_resource(RFIDBulkImportResource, "/api/rfid/import")
    api.add_resource(RFIDVerifyResource, "/api/rfid/verify")

    # Devices (LoRa gateways or phones)
    api.add_resource(
        LoRaDeviceListResource,
        "/api/lora/devices",
        "/api/devices",
        endpoint="devices",
    )
    api.add_resource(
        LoRaDeviceItemResource,
        "/api/lora/devices/<int:device_id>",
        "/api/devices/<int:device_id>",
        endpoint="device_item",
    )
    api.add_resource(
        LoRaMessageListResource,
        "/api/lora/messages",
        "/api/devices/messages",
        endpoint="device_messages",
    )

    # Checkins
    api.add_resource(CheckinListResource,   "/api/checkins")
    api.add_resource(CheckinItemResource,   "/api/checkins/<int:checkin_id>")
    api.add_resource(CheckinExportResource, "/api/checkins/export.csv")

    # Map
    api.add_resource(MapCheckpoints, "/api/map/checkpoints")
    api.add_resource(
        LoRaMapPoints,
        "/api/map/lora-points",
        "/api/map/device-points",
        endpoint="device_map_points",
    )

    # Documentation
    api.add_resource(ApiDocsListResource, "/api/docs")
    api.add_resource(ApiSpecResource, "/api/docs/<string:filename>")
