# app/resources/map.py
from __future__ import annotations
from flask_restful import Resource, reqparse

from app.utils.status import all_checkpoints_for_map, compute_team_statuses

_parser = reqparse.RequestParser(trim=True, bundle_errors=True)
_parser.add_argument("team_id", type=int, required=False, location="args")

class MapCheckpoints(Resource):
    """
    GET /api/map/checkpoints
      - No team_id: returns ALL checkpoints (id, name, easting, northing)
      - team_id=N: returns ONLY checkpoints that team N has checked in at
    """
    def get(self):
        args = _parser.parse_args()
        team_id = args.get("team_id")

        if team_id:
            status_summary = compute_team_statuses(team_id)
            return status_summary.get("checkpoints", []), 200

        cps = all_checkpoints_for_map()
        return [
            {
                "id": cp["id"],
                "name": cp["name"],
                "easting": cp["easting"],
                "northing": cp["northing"],
                "status": "not_found",
                "order": index,
                "location": cp.get("location"),
            }
            for index, cp in enumerate(cps)
        ], 200
