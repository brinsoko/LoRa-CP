"""Audit and submissions views render dict blobs for human eyes.
Jinja's built-in `tojson` ASCII-escapes everything (emoji, š/č/ž,
mojibake leftovers), producing ugly `\\u00f0` sequences on the page.
The `pretty_json` filter preserves UTF-8 while still HTML-escaping for
XSS safety. The scoring setup page (successor of the score-rules view)
renders field keys/labels via plain Jinja escaping, which must likewise
keep unicode glyphs intact."""

from __future__ import annotations

from datetime import datetime

from app.extensions import db
from app.models import AuditEvent, ScoreEntry
from tests.support import (
    add_membership,
    assign_team_group,
    create_checkpoint,
    create_competition,
    create_group,
    create_score_field,
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
        "Found JSON \\uXXXX escapes in the rendered HTML - pretty_json "
        "filter regressed to tojson"
    )
    # raw_fields with an emoji also round-trips into the rendered <pre>.
    assert "stage \U0001f3c1 finished" in body


def test_score_submissions_prefixes_team_with_number(client, app):
    """Operators scanning the submissions log want to see the team
    number next to the team name (e.g. '101 - Pitoni'), not just the
    name. Teams without a number assigned fall back to the bare name."""
    admin = create_user(username="num-admin", role="admin")
    comp = create_competition(name="Numbered Race")
    add_membership(admin, comp, role="admin")
    group = create_group(comp, name="Alpha", prefix="1xx")
    cp = create_checkpoint(comp, name="CP-Num")
    numbered = create_team(comp, name="Pitoni", number=101)
    unnumbered = create_team(comp, name="No Number")
    assign_team_group(numbered, group)
    assign_team_group(unnumbered, group)
    db.session.add_all(
        [
            ScoreEntry(
                competition_id=comp.id,
                team_id=numbered.id,
                checkpoint_id=cp.id,
                judge_user_id=admin.id,
                raw_fields={"points": 10},
                total=10.0,
                created_at=datetime(2026, 5, 20, 12, 0, 0),
            ),
            ScoreEntry(
                competition_id=comp.id,
                team_id=unnumbered.id,
                checkpoint_id=cp.id,
                judge_user_id=admin.id,
                raw_fields={"points": 5},
                total=5.0,
                created_at=datetime(2026, 5, 20, 12, 1, 0),
            ),
        ]
    )
    db.session.commit()
    login_as(client, admin, comp)

    resp = client.get("/scores/submissions")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    # Numbered team renders as "101 - Pitoni" (note: dash with spaces).
    assert "101 - Pitoni" in body, body[body.find("Pitoni") - 30 : body.find("Pitoni") + 30]
    # Unnumbered team renders as bare name (no leading separator/dash).
    assert "No Number" in body
    assert "- No Number" not in body, "team without a number should not have a dash prefix"


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


def test_scoring_setup_renders_unicode_field_names(client, app):
    """The scoring setup page (successor of the /scores/rules listing)
    shows each field's key and label in form inputs. Names with accents
    should render as those accents, not escapes."""
    admin = create_user(username="rules-admin")
    comp = create_competition(name="Diacritic Race")
    add_membership(admin, comp, role="admin")
    create_group(comp, name="Alpha", prefix="1xx")
    cp = create_checkpoint(comp, name="CP-Diacritic")
    create_score_field(cp, "točke", label="Točke", rule_type="multiplier", rule_params={"factor": 2})
    login_as(client, admin, comp)

    resp = client.get("/scores/setup")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")

    # Slovenian č appears as the glyph, not as a č escape.
    assert "točke" in body
    assert "Točke" in body
    # The rule params still render as JSON in the edit input (the
    # JSON-embedding tojson path is still allowed there).
    assert "factor" in body


def test_pretty_json_html_escapes_dangerous_values(app):
    """The filter must still escape HTML so a user-supplied raw_field
    value with a <script> tag can't break out of the <pre>."""
    with app.test_request_context():
        env = app.jinja_env
        template = env.from_string("{{ value | pretty_json }}")
        rendered = template.render(value={"name": "<script>alert(1)</script>"})
        assert "<script>" not in rendered, rendered
        assert "&lt;script&gt;" in rendered
