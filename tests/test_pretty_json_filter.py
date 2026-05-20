"""Audit, submissions, and score-rules views render dict blobs for
human eyes. Jinja's built-in `tojson` ASCII-escapes everything (emoji,
š/č/ž, mojibake leftovers), producing ugly `\\u00f0` sequences on the
page. The `pretty_json` filter preserves UTF-8 while still
HTML-escaping for XSS safety."""

from __future__ import annotations

from datetime import datetime

from app.extensions import db
from app.models import AuditEvent, ScoreEntry, ScoreRule
from tests.support import (
    add_membership,
    assign_team_group,
    create_checkpoint,
    create_competition,
    create_group,
    create_team,
    create_user,
    login_as,
)


def test_score_submissions_renders_emoji_in_team_name_and_fields(client, app):
    admin = create_user(username="emoji-admin", role="admin")
    comp = create_competition(name="Emoji Race")
    add_membership(admin, comp, role="admin")
    group = create_group(comp, name="Alpha", prefix="1xx")
    cp = create_checkpoint(comp, name="CP-Pretty")
    team = create_team(comp, name="Limwnce\U0001f380\U0001f34b\U0001f378", number=101)
    assign_team_group(team, group)
    db.session.add(
        ScoreEntry(
            competition_id=comp.id,
            team_id=team.id,
            checkpoint_id=cp.id,
            judge_user_id=admin.id,
            raw_fields={"note": "stage \U0001f3c1 finished", "points": 42},
            total=42.0,
            created_at=datetime(2026, 5, 20, 12, 0, 0),
        )
    )
    db.session.commit()
    login_as(client, admin, comp)

    resp = client.get("/scores/submissions")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")

    # Team name with three emojis renders as actual emoji glyphs, not
    # escape sequences.
    assert "Limwnce\U0001f380\U0001f34b\U0001f378" in body
    assert "\\u" not in body or "\\ud83" not in body, (
        "Found JSON \\uXXXX escapes in the rendered HTML — pretty_json "
        "filter regressed to tojson"
    )
    # raw_fields with an emoji also round-trips into the rendered <pre>.
    assert "stage \U0001f3c1 finished" in body


def test_audit_details_render_unicode_payload(client, app):
    admin = create_user(username="audit-admin", role="admin")
    comp = create_competition(name="Audit UTF8 Race")
    add_membership(admin, comp, role="admin")
    db.session.add(
        AuditEvent(
            competition_id=comp.id,
            event_type="team_renamed",
            entity_type="team",
            entity_id=1,
            summary="Renamed",
            details={"old": "Limwnce", "new": "Limwnce\U0001f380"},
            created_at=datetime(2026, 5, 20, 12, 0, 0),
        )
    )
    db.session.commit()
    login_as(client, admin, comp)

    resp = client.get("/audit/")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")

    # The emoji must appear as the actual glyph in the details <pre>.
    assert "Limwnce\U0001f380" in body
    # And no \uXXXX escapes for high-plane characters.
    assert "\\ud83" not in body, "Audit page still ASCII-escaping emojis"


def test_score_rules_pre_renders_unicode_field_names(client, app):
    """The score-rules listing shows each rule's JSON in a <pre>. Field
    names with accents should render as those accents, not escapes."""
    admin = create_user(username="rules-admin", role="admin")
    comp = create_competition(name="Diacritic Race")
    add_membership(admin, comp, role="admin")
    group = create_group(comp, name="Alpha", prefix="1xx")
    cp = create_checkpoint(comp, name="CP-Diacritic")
    db.session.add(
        ScoreRule(
            competition_id=comp.id,
            checkpoint_id=cp.id,
            group_id=group.id,
            rules={"field_rules": {"točke": {"type": "multiplier", "factor": 2}}},
        )
    )
    db.session.commit()
    login_as(client, admin, comp)

    resp = client.get("/scores/rules")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")

    # Slovenian č appears as the glyph, not as a č escape.
    assert "točke" in body
    # The JS-embedding tojson on the Load button is still allowed —
    # check we didn't accidentally break that path (data-rule attr).
    assert "data-rule=" in body


def test_pretty_json_html_escapes_dangerous_values(app):
    """The filter must still escape HTML so a user-supplied raw_field
    value with a <script> tag can't break out of the <pre>."""
    with app.test_request_context():
        env = app.jinja_env
        template = env.from_string("{{ value | pretty_json }}")
        rendered = template.render(value={"name": "<script>alert(1)</script>"})
        assert "<script>" not in rendered, rendered
        assert "&lt;script&gt;" in rendered
