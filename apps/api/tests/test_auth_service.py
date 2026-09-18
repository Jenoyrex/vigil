"""Tests for app.services.auth -- dashboard user login/session lifecycle
business logic (Phase 4D, F1). Route-level HTTP behavior (generic error
responses, rate limiting) is covered separately in test_auth_api.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.db.models import DashboardSession, OrganizationMembership, User
from app.security.passwords import hash_password
from app.security.sessions import hash_session_token
from app.services.auth import authenticate_and_create_session, revoke_session, validate_session


def test_authenticate_and_create_session_succeeds_with_correct_credentials(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    result = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email,
        password=active_dashboard_user.raw_password,
        session_ttl_hours=12,
    )
    assert result is not None
    assert result.session_token
    assert result.expires_at > datetime.now(UTC)


def test_authenticate_and_create_session_email_lookup_is_case_insensitive(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    result = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email.upper(),
        password=active_dashboard_user.raw_password,
        session_ttl_hours=12,
    )
    assert result is not None


def test_authenticate_and_create_session_persists_only_the_hash_never_the_raw_token(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    result = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email,
        password=active_dashboard_user.raw_password,
        session_ttl_hours=12,
    )
    assert result is not None

    row = db_session.query(DashboardSession).filter(
        DashboardSession.user_id == active_dashboard_user.user.id
    ).one()
    assert row.token_hash == hash_session_token(result.session_token)
    assert row.token_hash != result.session_token
    assert result.session_token not in row.token_hash


def test_authenticate_and_create_session_rejects_unknown_email(db_session: Session) -> None:
    result = authenticate_and_create_session(
        db_session, email="nobody@example.com", password="whatever", session_ttl_hours=12
    )
    assert result is None


def test_authenticate_and_create_session_rejects_wrong_password(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    result = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email,
        password="definitely not the password",
        session_ttl_hours=12,
    )
    assert result is None


def test_authenticate_and_create_session_rejects_inactive_user(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    active_dashboard_user.user.is_active = False
    db_session.commit()

    result = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email,
        password=active_dashboard_user.raw_password,
        session_ttl_hours=12,
    )
    assert result is None


def test_authenticate_and_create_session_rejects_user_with_no_membership(
    db_session: Session,
) -> None:
    password = "correct horse battery staple"
    user = User(
        email="orphan@example.com", hashed_password=hash_password(password), is_active=True
    )
    db_session.add(user)
    db_session.commit()

    result = authenticate_and_create_session(
        db_session, email=user.email, password=password, session_ttl_hours=12
    )
    assert result is None


def test_authenticate_and_create_session_rejects_user_with_no_password_set(
    db_session: Session,
) -> None:
    """A `users` row with `hashed_password = NULL` (the schema's default,
    matching every row before this phase's bootstrap change) must never
    authenticate against any presented password, including an empty one."""
    from test_models import make_organization, make_project

    org = make_organization(db_session)
    make_project(db_session, org)
    user = User(email="nopass@example.com", hashed_password=None, is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(OrganizationMembership(user_id=user.id, organization_id=org.id, role="owner"))
    db_session.commit()

    result = authenticate_and_create_session(
        db_session, email=user.email, password="", session_ttl_hours=12
    )
    assert result is None


def test_validate_session_succeeds_for_a_freshly_created_session(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    login_result = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email,
        password=active_dashboard_user.raw_password,
        session_ttl_hours=12,
    )
    assert login_result is not None

    session_result = validate_session(db_session, raw_token=login_result.session_token)
    assert session_result is not None
    assert session_result.user_id == active_dashboard_user.user.id
    assert session_result.email == active_dashboard_user.user.email


def test_validate_session_rejects_unknown_token(db_session: Session) -> None:
    assert validate_session(db_session, raw_token="not-a-real-token") is None


def test_validate_session_rejects_expired_session(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    from app.security.sessions import generate_session_token

    raw_token, token_hash = generate_session_token()
    expired = DashboardSession(
        user_id=active_dashboard_user.user.id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(expired)
    db_session.commit()

    assert validate_session(db_session, raw_token=raw_token) is None


def test_validate_session_rejects_revoked_session(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    login_result = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email,
        password=active_dashboard_user.raw_password,
        session_ttl_hours=12,
    )
    assert login_result is not None

    revoke_session(db_session, raw_token=login_result.session_token)

    assert validate_session(db_session, raw_token=login_result.session_token) is None


def test_validate_session_rejects_session_of_now_inactive_user(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    """A session created while the user was active must stop working the
    moment the user is deactivated -- validate_session re-checks
    `is_active` on every call, not only at login time."""
    login_result = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email,
        password=active_dashboard_user.raw_password,
        session_ttl_hours=12,
    )
    assert login_result is not None

    active_dashboard_user.user.is_active = False
    db_session.commit()

    assert validate_session(db_session, raw_token=login_result.session_token) is None


def test_revoke_session_marks_revoked_at(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    login_result = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email,
        password=active_dashboard_user.raw_password,
        session_ttl_hours=12,
    )
    assert login_result is not None

    revoke_session(db_session, raw_token=login_result.session_token)

    row = db_session.query(DashboardSession).filter(
        DashboardSession.user_id == active_dashboard_user.user.id
    ).one()
    assert row.revoked_at is not None


def test_revoke_session_is_idempotent(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    login_result = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email,
        password=active_dashboard_user.raw_password,
        session_ttl_hours=12,
    )
    assert login_result is not None

    revoke_session(db_session, raw_token=login_result.session_token)
    revoke_session(db_session, raw_token=login_result.session_token)  # must not raise

    row = db_session.query(DashboardSession).filter(
        DashboardSession.user_id == active_dashboard_user.user.id
    ).one()
    assert row.revoked_at is not None


def test_revoke_session_on_unknown_token_does_not_raise(db_session: Session) -> None:
    revoke_session(db_session, raw_token="not-a-real-token")  # must not raise


def test_login_issues_a_fresh_token_on_every_call(
    db_session: Session, active_dashboard_user: SimpleNamespace
) -> None:
    first = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email,
        password=active_dashboard_user.raw_password,
        session_ttl_hours=12,
    )
    second = authenticate_and_create_session(
        db_session,
        email=active_dashboard_user.user.email,
        password=active_dashboard_user.raw_password,
        session_ttl_hours=12,
    )
    assert first is not None
    assert second is not None
    assert first.session_token != second.session_token

    # Both sessions independently valid -- logging in again does not
    # invalidate a prior session (no single-session-per-user constraint).
    assert validate_session(db_session, raw_token=first.session_token) is not None
    assert validate_session(db_session, raw_token=second.session_token) is not None
