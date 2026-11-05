
"""
VoteEase test suite.

Run with:
    python -m pytest tests/test_app.py -v

Every test uses a temporary SQLite database and does not modify
the real database/voting.db file.
"""

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

import app as app_module  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    frozen_time = datetime.now() + timedelta(minutes=2)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen_time
            return frozen_time.replace(tzinfo=tz)

    def fake_get_conn():
        return sqlite3.connect(db_path)

    monkeypatch.setattr(app_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(app_module, "get_conn", fake_get_conn)

    import utils.db as db_module

    monkeypatch.setattr(db_module, "get_conn", fake_get_conn)

    monkeypatch.setattr(app_module, "ADMIN_PASS", "testadminpass")

    app_module.app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SECRET_KEY="test-secret",
    )

    app_module.init_db()

    try:
        with app_module.app.test_client() as test_client:
            yield test_client
    finally:
        os.close(db_fd)
        os.unlink(db_path)


def register(client, cid="0905CS241001", pwd="password123"):
    return client.post(
        "/register",
        data={
            "college_id": cid,
            "password": pwd,
            "confirm": pwd,
        },
        follow_redirects=True,
    )


def login(client, cid="0905CS241001", pwd="password123"):
    return client.post(
        "/login",
        data={
            "college_id": cid,
            "password": pwd,
        },
        follow_redirects=True,
    )


def create_election(conn, name="Test Election", active=True):
    """Create a future election eligible at the frozen application time."""
    now = datetime.now()

    start_time = (now + timedelta(minutes=1)).isoformat()
    end_time = (now + timedelta(minutes=30)).isoformat()

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO elections
            (name, is_active, start_time, end_time)
        VALUES (?, ?, ?, ?)
        """,
        (name, int(active), start_time, end_time),
    )

    return cur.lastrowid


def test_profile_election_period_formats_same_day_once():
    period = app_module.format_profile_election_period(
        "2026-09-25T19:39:42.710283",
        "2026-09-25T20:09:42.710283",
    )

    assert period == ("25 Sep 2026", "7:39 PM – 8:09 PM")


def test_profile_election_period_formats_both_dates_when_multiday():
    period = app_module.format_profile_election_period(
        "2026-09-25T23:39:42.710283",
        "2026-09-26T00:09:42.710283",
    )

    assert period == (
        "25 Sep 2026, 11:39 PM",
        "– 26 Sep 2026, 12:09 AM",
    )


@pytest.mark.parametrize(
    ("start_time", "end_time", "expected"),
    [
        (None, None, ("Date not set", None)),
        ("invalid", "2026-09-25T20:09:42.710283", (
            "Date not set",
            "– 25 Sep 2026, 8:09 PM",
        )),
        ("2026-09-25T19:39:42.710283", None, (
            "25 Sep 2026, 7:39 PM",
            None,
        )),
    ],
)
def test_profile_election_period_handles_missing_or_invalid_timestamps(
    start_time, end_time, expected
):
    assert app_module.format_profile_election_period(start_time, end_time) == expected


def create_candidate(conn, election_id, name="Candidate A"):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO candidates (election_id, name, photo)
        VALUES (?, ?, ?)
        """,
        (election_id, name, "candidate.png"),
    )

    return cur.lastrowid


def get_student_id(conn, college_id="0905CS241001"):
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM students WHERE college_id = ?",
        (college_id,),
    )
    row = cur.fetchone()

    assert row is not None, f"Student {college_id} was not found."
    return row[0]


def count_votes(conn, election_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM votes WHERE election_id = ?",
        (election_id,),
    )
    return cur.fetchone()[0]


def get_votes():
    conn = app_module.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT election_id, student_id, candidate_id FROM votes ORDER BY id"
        )
        return cur.fetchall()
    finally:
        conn.close()


def test_home_page_loads(client):
    response = client.get("/")
    assert response.status_code == 200


def test_register_creates_student_and_redirects_to_login(client):
    response = register(client)

    assert response.status_code == 200
    assert (
        b"login" in response.request.path.encode()
        or response.request.path == "/login"
    )


def test_register_duplicate_college_id_rejected(client):
    register(client)

    response = register(client)

    assert b"already registered" in response.data


def test_register_password_mismatch_rejected(client):
    response = client.post(
        "/register",
        data={
            "college_id": "0905CS241002",
            "password": "abcdef",
            "confirm": "xyz123",
        },
        follow_redirects=True,
    )

    assert b"do not match" in response.data


def test_login_wrong_password_rejected(client):
    register(client)

    response = login(client, pwd="wrongpassword")

    assert b"Invalid credentials" in response.data


def test_login_success_redirects_to_vote_flow(client):
    register(client)

    response = login(client)

    assert response.status_code == 200


def test_admin_requires_password(client):
    response = client.post(
        "/admin",
        data={
            "admin_login": "1",
            "password": "wrong",
        },
        follow_redirects=True,
    )

    assert b"Wrong admin password" in response.data


@pytest.mark.parametrize("configured_password", [None, ""])
def test_admin_password_configuration_must_be_present_and_nonempty(
    configured_password,
):
    with pytest.raises(RuntimeError, match="ADMIN_PASS must be configured"):
        app_module._validate_admin_password_config(configured_password)


def test_admin_password_configuration_rejects_whitespace():
    with pytest.raises(RuntimeError, match="ADMIN_PASS must be configured"):
        app_module._validate_admin_password_config(" \t ")


@pytest.mark.parametrize("submitted_password", ["", " ", "\t \n"])
def test_admin_rejects_blank_or_whitespace_password(client, submitted_password):
    response = client.post(
        "/admin",
        data={
            "admin_login": "1",
            "password": submitted_password,
        },
    )

    assert b"Wrong admin password" in response.data
    with client.session_transaction() as session:
        assert "admin" not in session


def test_admin_login_requires_csrf_token(client):
    csrf_enabled = app_module.app.config["WTF_CSRF_ENABLED"]
    app_module.app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post(
            "/admin",
            data={
                "admin_login": "1",
                "password": app_module.ADMIN_PASS,
            },
        )
    finally:
        app_module.app.config["WTF_CSRF_ENABLED"] = csrf_enabled

    assert response.status_code == 400
    with client.session_transaction() as session:
        assert "admin" not in session


def test_admin_login_success(client):
    response = client.post(
        "/admin",
        data={
            "admin_login": "1",
            "password": app_module.ADMIN_PASS,
        },
        follow_redirects=True,
    )

    assert b"Admin Dashboard" in response.data
    with client.session_transaction() as session:
        assert session.get("admin") is True


def test_database_prevents_duplicate_vote_for_same_election(client):
    """The database must reject a second vote for the same student/election."""
    register(client)

    conn = app_module.get_conn()

    try:
        election_id = create_election(conn)
        candidate_id = create_candidate(conn, election_id)
        student_id = get_student_id(conn)

        conn.commit()

        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO votes (election_id, student_id, candidate_id)
            VALUES (?, ?, ?)
            """,
            (election_id, student_id, candidate_id),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            cur.execute(
                """
                INSERT INTO votes (election_id, student_id, candidate_id)
                VALUES (?, ?, ?)
                """,
                (election_id, student_id, candidate_id),
            )
            conn.commit()
    finally:
        conn.close()


def test_vote_rejects_candidate_from_another_election(client):
    """A vote must not be recorded for a candidate in another election."""
    register(client)

    conn = app_module.get_conn()

    try:
        active_election_id = create_election(
            conn,
            name="Active Election",
            active=True,
        )
        other_election_id = create_election(
            conn,
            name="Other Election",
            active=False,
        )

        wrong_candidate_id = create_candidate(
            conn,
            other_election_id,
            name="Candidate From Another Election",
        )

        conn.commit()
    finally:
        conn.close()

    login(client)

    response = client.post(
        "/vote",
        data={"candidate": str(wrong_candidate_id)},
        follow_redirects=False,
    )

    assert response.status_code == 302

    conn = app_module.get_conn()

    try:
        assert count_votes(conn, active_election_id) == 0
    finally:
        conn.close()


def test_vote_rejects_nonexistent_candidate(client):
    """A vote must not be recorded for a candidate ID that does not exist."""
    register(client)

    conn = app_module.get_conn()

    try:
        election_id = create_election(conn)
        conn.commit()

        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM candidates WHERE id = ?",
            (-999,),
        )
        assert cur.fetchone() is None
    finally:
        conn.close()

    login(client)

    response = client.post(
        "/vote",
        data={"candidate": "-999"},
        follow_redirects=False,
    )

    assert response.status_code == 302

    conn = app_module.get_conn()

    try:
        assert count_votes(conn, election_id) == 0
    finally:
        conn.close()


def test_vote_records_valid_vote(client):
    register(client)
    conn = app_module.get_conn()
    try:
        election_id = create_election(conn)
        candidate_id = create_candidate(conn, election_id)
        student_id = get_student_id(conn)
        conn.commit()
    finally:
        conn.close()

    login(client)
    response = client.post(
        "/vote",
        data={"candidate": str(candidate_id)},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/thanks"
    assert get_votes() == [(election_id, student_id, candidate_id)]


def test_vote_rejects_candidate_from_another_election_and_preserves_state(client):
    register(client)
    conn = app_module.get_conn()
    try:
        active_election_id = create_election(
            conn,
            name="Active Election",
            active=True,
        )
        other_election_id = create_election(
            conn,
            name="Other Election",
            active=False,
        )
        other_candidate_id = create_candidate(
            conn,
            other_election_id,
            name="Other Candidate",
        )
        conn.commit()
    finally:
        conn.close()

    login(client)
    response = client.post(
        "/vote",
        data={"candidate": str(other_candidate_id)},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/vote"
    assert count_votes_for_election(active_election_id) == 0
    assert get_votes() == []


def test_vote_rejects_missing_candidate_and_preserves_state(client):
    register(client)
    conn = app_module.get_conn()
    try:
        election_id = create_election(conn)
        conn.commit()
    finally:
        conn.close()

    login(client)
    response = client.post("/vote", data={}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"] == "/vote"
    assert count_votes_for_election(election_id) == 0
    assert get_votes() == []


def test_database_rejects_duplicate_vote_and_keeps_one_vote(client):
    register(client)
    conn = app_module.get_conn()
    try:
        election_id = create_election(conn)
        candidate_id = create_candidate(conn, election_id)
        student_id = get_student_id(conn)
        conn.commit()

        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO votes (election_id, student_id, candidate_id)
            VALUES (?, ?, ?)
            """,
            (election_id, student_id, candidate_id),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            cur.execute(
                """
                INSERT INTO votes (election_id, student_id, candidate_id)
                VALUES (?, ?, ?)
                """,
                (election_id, student_id, candidate_id),
            )
        conn.rollback()
    finally:
        conn.close()

    assert get_votes() == [(election_id, student_id, candidate_id)]


def test_unrelated_integrity_error_is_not_treated_as_a_successful_vote(client):
    register(client)
    conn = app_module.get_conn()
    try:
        election_id = create_election(conn)
        candidate_id = create_candidate(conn, election_id)
        conn.execute(
            """
            CREATE TRIGGER reject_test_vote
            BEFORE INSERT ON votes
            BEGIN
                SELECT RAISE(ABORT, 'simulated integrity failure');
            END
            """
        )
        conn.commit()
    finally:
        conn.close()

    login(client)
    with pytest.raises(sqlite3.IntegrityError, match="simulated integrity failure"):
        client.post(
            "/vote",
            data={"candidate": str(candidate_id)},
            follow_redirects=False,
        )

    assert get_votes() == []


def test_unauthenticated_student_cannot_vote(client):
    conn = app_module.get_conn()
    try:
        election_id = create_election(conn)
        candidate_id = create_candidate(conn, election_id)
        conn.commit()
    finally:
        conn.close()

    response = client.post(
        "/vote",
        data={"candidate": str(candidate_id)},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/login"
    assert count_votes_for_election(election_id) == 0
    assert get_votes() == []


def test_vote_post_requires_csrf_token(client):
    register(client)
    conn = app_module.get_conn()
    try:
        election_id = create_election(conn)
        candidate_id = create_candidate(conn, election_id)
        conn.commit()
    finally:
        conn.close()

    login(client)
    csrf_enabled = app_module.app.config["WTF_CSRF_ENABLED"]
    app_module.app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post(
            "/vote",
            data={"candidate": str(candidate_id)},
            follow_redirects=False,
        )
    finally:
        app_module.app.config["WTF_CSRF_ENABLED"] = csrf_enabled

    assert response.status_code == 400
    assert count_votes_for_election(election_id) == 0
    assert get_votes() == []


def test_student_cannot_vote_twice(client):
    register(client)
    conn = app_module.get_conn()
    try:
        election_id = create_election(conn)
        candidate_id = create_candidate(conn, election_id)
        student_id = get_student_id(conn)
        conn.commit()
    finally:
        conn.close()

    login(client)
    first_response = client.post(
        "/vote",
        data={"candidate": str(candidate_id)},
        follow_redirects=False,
    )
    assert first_response.status_code == 302
    assert first_response.headers["Location"] == "/thanks"
    assert get_votes() == [(election_id, student_id, candidate_id)]

    second_response = client.post(
        "/vote",
        data={"candidate": str(candidate_id)},
        follow_redirects=False,
    )
    assert second_response.status_code == 302
    assert second_response.headers["Location"] == "/thanks"
    assert get_votes() == [(election_id, student_id, candidate_id)]


def count_votes_for_election(election_id):
    conn = app_module.get_conn()
    try:
        return count_votes(conn, election_id)
    finally:
        conn.close()


def get_password_hash(college_id="0905CS241001"):
    conn = app_module.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT password_hash FROM students WHERE college_id = ?",
            (college_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]
    finally:
        conn.close()


def test_student_profile_requires_authentication(client):
    response = client.get("/student/profile", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"] == "/login"


def test_admin_is_not_authorized_as_a_student(client):
    client.post(
        "/admin",
        data={"admin_login": "1", "password": app_module.ADMIN_PASS},
    )

    response = client.get("/student/profile", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"] == "/login"


def test_profile_displays_only_authenticated_student_information(client):
    register(client, pwd="OriginalPass123!")
    register(
        client,
        cid="0905CS241002",
        pwd="OtherPass123!",
    )
    conn = app_module.get_conn()
    try:
        conn.execute(
            "UPDATE students SET name = ?, email = ? WHERE college_id = ?",
            ("Own Student", "own@example.edu", "0905CS241001"),
        )
        conn.execute(
            "UPDATE students SET name = ?, email = ? WHERE college_id = ?",
            ("Other Student", "other@example.edu", "0905CS241002"),
        )
        conn.commit()
    finally:
        conn.close()

    login(client, pwd="OriginalPass123!")
    response = client.get("/student/profile")

    assert response.status_code == 200
    assert b"Own Student" in response.data
    assert b"0905CS241001" in response.data
    assert b"own@example.edu" in response.data
    assert b"Other Student" not in response.data
    assert b"other@example.edu" not in response.data
    assert get_password_hash().encode() not in response.data


def test_password_change_updates_hash_and_authenticates_new_password(client):
    old_password = "OriginalPass123!"
    new_password = "UpdatedPassword456!"
    register(client, pwd=old_password)
    old_hash = get_password_hash()
    login(client, pwd=old_password)

    response = client.post(
        "/student/profile",
        data={
            "current_password": old_password,
            "new_password": new_password,
            "confirm_password": new_password,
        },
        follow_redirects=True,
    )

    updated_hash = get_password_hash()
    assert response.status_code == 200
    assert b"Your password was changed successfully." in response.data
    assert old_password.encode() not in response.data
    assert new_password.encode() not in response.data
    assert old_hash != updated_hash
    assert app_module.verify_password(updated_hash, new_password)
    assert not app_module.verify_password(updated_hash, old_password)

    client.post("/logout")
    old_login = login(client, pwd=old_password)
    assert b"Invalid credentials" in old_login.data
    new_login = login(client, pwd=new_password)
    assert b"No Active Election" in new_login.data


@pytest.mark.parametrize(
    ("password_data", "message"),
    [
        (
            {
                "current_password": "IncorrectPass123!",
                "new_password": "UpdatedPassword456!",
                "confirm_password": "UpdatedPassword456!",
            },
            b"Current password is incorrect.",
        ),
        (
            {
                "current_password": "OriginalPass123!",
                "new_password": "UpdatedPassword456!",
                "confirm_password": "DifferentPassword789!",
            },
            b"New passwords do not match.",
        ),
        (
            {
                "current_password": "",
                "new_password": "",
                "confirm_password": "",
            },
            b"All password fields are required.",
        ),
        (
            {
                "current_password": "OriginalPass123!",
                "new_password": "weak1",
                "confirm_password": "weak1",
            },
            b"at least 12 characters",
        ),
        (
            {
                "current_password": "OriginalPass123!",
                "new_password": "OriginalPass123!",
                "confirm_password": "OriginalPass123!",
            },
            b"different from the current password",
        ),
    ],
)
def test_invalid_password_changes_are_rejected(client, password_data, message):
    register(client, pwd="OriginalPass123!")
    original_hash = get_password_hash()
    login(client, pwd="OriginalPass123!")

    response = client.post(
        "/student/profile",
        data=password_data,
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert message in response.data
    assert get_password_hash() == original_hash


def test_password_change_ignores_forged_student_id(client):
    password = "OriginalPass123!"
    register(client, pwd=password)
    register(client, cid="0905CS241002", pwd=password)
    conn = app_module.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM students WHERE college_id = ?",
            ("0905CS241002",),
        )
        other_student_id = cur.fetchone()[0]
    finally:
        conn.close()

    login(client, pwd=password)
    response = client.post(
        "/student/profile",
        data={
            "student_id": str(other_student_id),
            "current_password": password,
            "new_password": "NewPassword456!",
            "confirm_password": "NewPassword456!",
        },
        follow_redirects=True,
    )

    assert b"Your password was changed successfully." in response.data
    assert app_module.verify_password(get_password_hash(), "NewPassword456!")
    assert app_module.verify_password(
        get_password_hash("0905CS241002"),
        password,
    )


def test_student_cannot_edit_identity_fields_through_profile(client):
    password = "OriginalPass123!"
    register(client, pwd=password)
    conn = app_module.get_conn()
    try:
        conn.execute(
            "UPDATE students SET name = ?, email = ? WHERE college_id = ?",
            ("Admin Managed", "managed@example.edu", "0905CS241001"),
        )
        conn.commit()
    finally:
        conn.close()

    login(client, pwd=password)
    client.post(
        "/student/profile",
        data={
            "college_id": "0905CS241999",
            "name": "Student Edited",
            "email": "student@example.edu",
            "current_password": password,
            "new_password": "UpdatedPassword456!",
            "confirm_password": "UpdatedPassword456!",
        },
    )

    conn = app_module.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT college_id, name, email FROM students WHERE id = ?",
            (get_student_id(conn),),
        )
        assert cur.fetchone() == (
            "0905CS241001",
            "Admin Managed",
            "managed@example.edu",
        )
    finally:
        conn.close()


def test_password_change_requires_csrf_token(client):
    register(client, pwd="OriginalPass123!")
    login(client, pwd="OriginalPass123!")
    original_hash = get_password_hash()
    csrf_enabled = app_module.app.config["WTF_CSRF_ENABLED"]
    app_module.app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post(
            "/student/profile",
            data={
                "current_password": "OriginalPass123!",
                "new_password": "UpdatedPassword456!",
                "confirm_password": "UpdatedPassword456!",
            },
        )
    finally:
        app_module.app.config["WTF_CSRF_ENABLED"] = csrf_enabled

    assert response.status_code == 400
    assert get_password_hash() == original_hash


def test_password_change_invalidates_other_student_sessions(client):
    password = "OriginalPass123!"
    register(client, pwd=password)
    second_client = app_module.app.test_client()
    login(client, pwd=password)
    login(second_client, pwd=password)

    client.post(
        "/student/profile",
        data={
            "current_password": password,
            "new_password": "UpdatedPassword456!",
            "confirm_password": "UpdatedPassword456!",
        },
    )
    response = second_client.get("/student/profile", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"] == "/login"


def test_password_change_invalidates_other_sessions_before_voting(client):
    password = "OriginalPass123!"
    register(client, pwd=password)
    conn = app_module.get_conn()
    try:
        election_id = create_election(conn)
        candidate_id = create_candidate(conn, election_id)
        conn.commit()
    finally:
        conn.close()

    second_client = app_module.app.test_client()
    login(client, pwd=password)
    login(second_client, pwd=password)
    client.post(
        "/student/profile",
        data={
            "current_password": password,
            "new_password": "UpdatedPassword456!",
            "confirm_password": "UpdatedPassword456!",
        },
    )

    response = second_client.post(
        "/vote",
        data={"candidate": str(candidate_id)},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/login"
    assert count_votes_for_election(election_id) == 0


def test_password_change_does_not_invalidate_admin_session(client):
    admin_client = app_module.app.test_client()
    admin_client.post(
        "/admin",
        data={"admin_login": "1", "password": app_module.ADMIN_PASS},
    )
    register(client, pwd="OriginalPass123!")
    login(client, pwd="OriginalPass123!")

    response = client.post(
        "/student/profile",
        data={
            "current_password": "OriginalPass123!",
            "new_password": "UpdatedPassword456!",
            "confirm_password": "UpdatedPassword456!",
        },
    )
    admin_response = admin_client.get("/admin")

    assert response.status_code == 302
    assert admin_response.status_code == 200
    assert b"Admin Dashboard" in admin_response.data


def test_password_update_rejects_concurrent_password_version_change(
    client,
    monkeypatch,
):
    password = "OriginalPass123!"
    register(client, pwd=password)
    login(client, pwd=password)
    original_hash = get_password_hash()
    real_hash_password = app_module.hash_password

    def simulate_concurrent_password_change(new_password):
        conn = app_module.get_conn()
        try:
            conn.execute(
                """
                UPDATE students
                SET password_version = password_version + 1
                WHERE college_id = ?
                """,
                ("0905CS241001",),
            )
            conn.commit()
        finally:
            conn.close()
        return real_hash_password(new_password)

    monkeypatch.setattr(
        app_module,
        "hash_password",
        simulate_concurrent_password_change,
    )
    response = client.post(
        "/student/profile",
        data={
            "current_password": password,
            "new_password": "UpdatedPassword456!",
            "confirm_password": "UpdatedPassword456!",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert get_password_hash() == original_hash


def test_profile_shows_only_participation_without_ballot_details(client):
    register(client, pwd="OriginalPass123!")
    register(
        client,
        cid="0905CS241002",
        pwd="OtherPass123!",
    )
    conn = app_module.get_conn()
    now = datetime.now() + timedelta(minutes=2)
    active_start = (now - timedelta(minutes=1)).isoformat()
    active_end = (now + timedelta(minutes=30)).isoformat()
    ended_start = (now - timedelta(hours=2)).isoformat()
    ended_end = (now - timedelta(hours=1)).isoformat()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO elections (name, is_active, start_time, end_time)
            VALUES (?, 1, ?, ?)
            """,
            ("Current Election", active_start, active_end),
        )
        active_election_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO elections (name, is_active, start_time, end_time)
            VALUES (?, 0, ?, ?)
            """,
            ("Completed Election", ended_start, ended_end),
        )
        completed_election_id = cur.lastrowid
        active_candidate_id = 987654321
        completed_candidate_id = 987654322
        cur.execute(
            """
            INSERT INTO candidates (id, election_id, name, photo)
            VALUES (?, ?, ?, ?)
            """,
            (
                active_candidate_id,
                active_election_id,
                "Secret Candidate Name",
                "candidate.png",
            ),
        )
        cur.execute(
            """
            INSERT INTO candidates (id, election_id, name, photo)
            VALUES (?, ?, ?, ?)
            """,
            (
                completed_candidate_id,
                completed_election_id,
                "Another Secret Candidate",
                "candidate.png",
            ),
        )
        cur.execute(
            "SELECT id FROM students WHERE college_id = ?",
            ("0905CS241002",),
        )
        other_student_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO votes (election_id, student_id, candidate_id)
            VALUES (?, ?, ?)
            """,
            (active_election_id, other_student_id, active_candidate_id),
        )
        cur.execute(
            "SELECT id FROM students WHERE college_id = ?",
            ("0905CS241001",),
        )
        own_student_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO votes (election_id, student_id, candidate_id)
            VALUES (?, ?, ?)
            """,
            (completed_election_id, own_student_id, completed_candidate_id),
        )
        conn.commit()
    finally:
        conn.close()

    login(client, pwd="OriginalPass123!")
    response = client.get("/student/profile")

    assert response.status_code == 200
    assert b"Current Election" in response.data
    assert b"Completed Election" in response.data
    assert b"Active" in response.data
    assert b"Completed" in response.data
    assert b"Voted" in response.data
    assert b"Not Participated" in response.data
    assert b"Secret Candidate Name" not in response.data
    assert b"Another Secret Candidate" not in response.data
    assert b"candidate_id" not in response.data
    assert str(active_candidate_id).encode() not in response.data
    assert str(completed_candidate_id).encode() not in response.data


def test_admin_student_list_preserves_account_actions_without_identity_editing(client):
    register(client)
    conn = app_module.get_conn()
    try:
        student_id = get_student_id(conn)
        conn.execute(
            "UPDATE students SET name = ?, email = ? WHERE id = ?",
            ("Existing Name", "existing@example.edu", student_id),
        )
        conn.commit()
    finally:
        conn.close()

    client.post(
        "/admin",
        data={"admin_login": "1", "password": app_module.ADMIN_PASS},
    )
    response = client.get("/admin")
    assert response.status_code == 200
    assert b"0905CS241001" in response.data
    assert b"No Election" in response.data
    assert b"/delete_student/0905CS241001" in response.data
    assert b"student-profile-" not in response.data
    assert b"Name for 0905CS241001" not in response.data
    assert b"Email for 0905CS241001" not in response.data
    assert b"Save details" not in response.data

    response = client.post(f"/admin/students/{student_id}/profile")
    assert response.status_code == 404

    conn = app_module.get_conn()
    try:
        row = conn.execute(
            "SELECT name, email FROM students WHERE id = ?",
            (student_id,),
        ).fetchone()
        assert row == ("Existing Name", "existing@example.edu")
    finally:
        conn.close()


def test_student_schema_migration_adds_identity_and_password_version_columns(client):
    conn = app_module.get_conn()
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(students)").fetchall()
        }
        assert {"name", "email", "password_version"} <= columns
        app_module.init_db()
        migrated_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(students)").fetchall()
        }
        assert migrated_columns == columns
    finally:
        conn.close()


def test_init_db_migrates_existing_student_table_without_losing_records(monkeypatch):
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)

    def fake_get_conn():
        return sqlite3.connect(db_path)

    monkeypatch.setattr(app_module, "get_conn", fake_get_conn)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE students (
                id INTEGER PRIMARY KEY,
                college_id TEXT UNIQUE,
                password_hash TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO students (college_id, password_hash) VALUES (?, ?)",
            ("legacy-student", "existing-hash"),
        )
        conn.commit()
        conn.close()

        app_module.init_db()

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """
            SELECT college_id, password_hash, name, email, password_version
            FROM students
            """
        ).fetchone()
        conn.close()
        assert row == ("legacy-student", "existing-hash", None, None, 0)
    finally:
        os.unlink(db_path)
