import hmac
import re

from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_wtf import CSRFProtect
from utils.db import get_conn
from utils.security import hash_password, verify_password
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from PIL import Image
import sqlite3, os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ---------------- SECRETS (env-var backed, no hardcoding) ----------------
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax"
)
ADMIN_PASS = os.environ.get("ADMIN_PASS")  # override this in production!


def _validate_admin_password_config(password):
    if password is None or not password.strip():
        raise RuntimeError(
            "ADMIN_PASS must be configured with a non-empty, non-whitespace value."
        )
    return password


ADMIN_PASS = _validate_admin_password_config(ADMIN_PASS)

# ---------------- CSRF ----------------
csrf = CSRFProtect(app)

# ---------------- CONFIG ----------------
UPLOAD_FOLDER = "static/photos"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5MB upload cap


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


# ---------------- HELPERS ----------------
def row_to_dict(row):
    return dict(row) if row else None


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def format_profile_election_period(start_value, end_value):
    def parse_datetime(value):
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def format_date(value):
        return f"{value.day} {value:%b %Y}"

    def format_time(value):
        return value.strftime("%I:%M %p").lstrip("0")

    def format_datetime(value):
        return f"{format_date(value)}, {format_time(value)}"

    start = parse_datetime(start_value)
    end = parse_datetime(end_value)

    if start and end and start.date() == end.date():
        return (
            format_date(start),
            f"{format_time(start)} – {format_time(end)}",
        )

    return (
        format_datetime(start) if start else "Date not set",
        f"– {format_datetime(end)}" if end else None,
    )


def get_authenticated_student():
    if session.get("admin") is True:
        return None

    session_student = session.get("student")
    if not isinstance(session_student, dict):
        return None

    student_id = session_student.get("id")
    if type(student_id) is not int:
        session.clear()
        return None

    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, college_id, name, email, password_version
            FROM students
            WHERE id = ?
            """,
            (student_id,),
        )
        student = cur.fetchone()
    finally:
        conn.close()

    if student is None:
        session.clear()
        return None

    session_version = session_student.get("password_version", 0)
    if (
        type(session_version) is not int
        or session_version != student["password_version"]
    ):
        session.clear()
        return None

    return dict(student)


def expire_elections():
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute(
        "UPDATE elections SET is_active=0 WHERE is_active=1 AND end_time < ?",
        (now,)
    )
    conn.commit()
    conn.close()


# ---------------- NO CACHE ----------------
@app.after_request
def after_request(response):
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------- LOGOUT ----------------
@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- DB INIT ----------------
def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript("""
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY,
        college_id TEXT UNIQUE,
        password_hash TEXT,
        name TEXT,
        email TEXT,
        password_version INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS elections (
        id INTEGER PRIMARY KEY,
        name TEXT,
        is_active INTEGER DEFAULT 1,
        start_time TEXT,
        end_time TEXT
    );

    CREATE TABLE IF NOT EXISTS candidates (
        id INTEGER PRIMARY KEY,
        election_id INTEGER NOT NULL,
        name TEXT,
        photo TEXT,
        UNIQUE(election_id, id),
        FOREIGN KEY (election_id) REFERENCES elections(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS votes (
        id INTEGER PRIMARY KEY,
        election_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(election_id, student_id),
        FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
        FOREIGN KEY (election_id) REFERENCES elections(id) ON DELETE CASCADE,
        FOREIGN KEY (election_id, candidate_id) REFERENCES candidates(election_id, id) ON DELETE CASCADE
    );
    """)

    student_columns = {
        row[1] for row in cur.execute("PRAGMA table_info(students)").fetchall()
    }
    for column, definition in (
        ("name", "TEXT"),
        ("email", "TEXT"),
        ("password_version", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in student_columns:
            cur.execute(f"ALTER TABLE students ADD COLUMN {column} {definition}")

    conn.commit()
    conn.close()


def seed_students():
    conn = get_conn()
    cur = conn.cursor()
    for i in range(1, 65):
        cid = f"0905CS241{str(i).zfill(3)}"
        cur.execute(
            "INSERT OR IGNORE INTO students (college_id,password_hash) VALUES (?,?)",
            (cid, hash_password(f"{cid}#"))
        )
    conn.commit()
    conn.close()


# ---------------- HOME ----------------
@app.route("/")
@app.route("/home")
def home():
    expire_elections()
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM elections WHERE is_active=1")
    active_elections = [dict(r) for r in cur.fetchall()]
    conn.close()
    for e in active_elections:
        e["status"] = "Active"
    return render_template("home.html", active_elections=active_elections)


# ---------------- REGISTER ----------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        cid = request.form.get("college_id", "").strip()
        pwd = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not cid or not pwd:
            flash("All fields are required", "danger")
            return redirect(url_for("register"))

        if pwd != confirm:
            flash("Passwords do not match", "danger")
            return redirect(url_for("register"))

        if len(pwd) < 6:
            flash("Password must be at least 6 characters", "danger")
            return redirect(url_for("register"))

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM students WHERE college_id=?", (cid,))
        if cur.fetchone():
            flash("College ID already registered", "danger")
            conn.close()
            return redirect(url_for("register"))

        try:
            cur.execute(
                "INSERT INTO students (college_id, password_hash) VALUES (?,?)",
                (cid, hash_password(pwd))
            )
            conn.commit()
        except sqlite3.IntegrityError:
            flash("College ID already registered", "danger")
            conn.close()
            return redirect(url_for("register"))

        conn.close()
        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


# ---------------- LOGIN ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        cid = request.form["college_id"]
        pwd = request.form["password"]

        conn = get_conn()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, college_id, password_hash, password_version
            FROM students
            WHERE college_id = ?
            """,
            (cid,),
        )
        student = row_to_dict(cur.fetchone())
        conn.close()

        if not student or not verify_password(student["password_hash"], pwd):
            flash("Invalid credentials", "danger")
            return redirect(url_for("login"))

        session.clear()
        session["student"] = {
            "id": student["id"],
            "college_id": cid,
            "password_version": student["password_version"],
        }
        return redirect(url_for("vote"))

    return render_template("login.html")


# ---------------- VOTE ----------------
@app.route("/vote", methods=["GET", "POST"])
def vote():
    student = get_authenticated_student()
    if student is None:
        return redirect(url_for("login"))

    expire_elections()

    sid = student["id"]
    now = datetime.now().isoformat()

    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT * FROM elections
        WHERE is_active=1 AND start_time<=? AND end_time>=?
        ORDER BY id
    """, (now, now))
    active_elections = [row_to_dict(r) for r in cur.fetchall()]

    if len(active_elections) != 1:
        conn.close()
        return render_template("election.html")

    election = active_elections[0]

    cur.execute(
        "SELECT 1 FROM votes WHERE election_id=? AND student_id=?",
        (election["id"], sid)
    )
    has_voted = cur.fetchone() is not None

    if request.method == "POST":
        if has_voted:
            conn.close()
            return redirect(url_for("thanks"))

        candidate_id = request.form.get("candidate")
        if not candidate_id:
            conn.close()
            flash("Please select a candidate", "danger")
            return redirect(url_for("vote"))

        cur.execute(
            """
            SELECT 1
            FROM candidates
            WHERE id = ? AND election_id = ?
            """,
            (candidate_id, election["id"]),
        )
        if cur.fetchone() is None:
            conn.close()
            flash("Invalid candidate selection", "danger")
            return redirect(url_for("vote"))

        try:
            cur.execute(
                "INSERT INTO votes (election_id,student_id,candidate_id) VALUES (?,?,?)",
                (election["id"], sid, candidate_id)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            try:
                cur.execute(
                    "SELECT 1 FROM votes WHERE election_id=? AND student_id=?",
                    (election["id"], sid),
                )
                duplicate_vote = cur.fetchone() is not None
            finally:
                conn.close()

            if duplicate_vote:
                return redirect(url_for("thanks"))
            raise

        conn.close()
        return redirect(url_for("thanks"))

    cur.execute("SELECT * FROM candidates WHERE election_id=?", (election["id"],))
    candidates = rows_to_dicts(cur.fetchall())
    conn.close()

    return render_template(
        "vote.html",
        election=election,
        candidates=candidates,
        has_voted=has_voted,
        end_time=election["end_time"]
    )


# ---------------- STUDENT PROFILE ----------------
@app.route("/student/profile", methods=["GET", "POST"])
def student_profile():
    student = get_authenticated_student()
    if student is None:
        return redirect(url_for("login"))

    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_password or not new_password or not confirm_password:
            flash("All password fields are required.", "danger")
            return redirect(url_for("student_profile"))

        if new_password != confirm_password:
            flash("New passwords do not match.", "danger")
            return redirect(url_for("student_profile"))

        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT password_hash FROM students WHERE id = ?",
                (student["id"],),
            )
            row = cur.fetchone()
            if row is None:
                session.clear()
                return redirect(url_for("login"))

            if not verify_password(row[0], current_password):
                flash("Current password is incorrect.", "danger")
                return redirect(url_for("student_profile"))

            if new_password == current_password:
                flash("New password must be different from the current password.", "danger")
                return redirect(url_for("student_profile"))

            if (
                len(new_password) < 12
                or not re.search(r"[A-Za-z]", new_password)
                or not re.search(r"\d", new_password)
            ):
                flash(
                    "New password must be at least 12 characters and include "
                    "at least one letter and one number.",
                    "danger",
                )
                return redirect(url_for("student_profile"))

            cur.execute(
                """
                UPDATE students
                SET password_hash = ?, password_version = password_version + 1
                WHERE id = ? AND password_hash = ? AND password_version = ?
                """,
                (
                    hash_password(new_password),
                    student["id"],
                    row[0],
                    student["password_version"],
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                flash("Password could not be updated. Please try again.", "danger")
                return redirect(url_for("student_profile"))
            conn.commit()
        finally:
            conn.close()

        session["student"]["password_version"] = student["password_version"] + 1
        flash("Your password was changed successfully.", "success")
        return redirect(url_for("student_profile"))

    now = datetime.now().isoformat()
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                e.name,
                e.start_time,
                e.end_time,
                CASE
                    WHEN e.is_active = 1
                         AND e.start_time <= ?
                         AND e.end_time >= ?
                        THEN 'Active'
                    WHEN e.is_active = 0
                         OR (e.end_time IS NOT NULL AND e.end_time < ?)
                        THEN 'Completed'
                    ELSE 'Upcoming'
                END AS election_status,
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM votes AS v
                        WHERE v.election_id = e.id
                          AND v.student_id = ?
                    )
                        THEN 'Voted'
                    ELSE 'Not Participated'
                END AS participation_status
            FROM elections AS e
            ORDER BY e.start_time DESC, e.id DESC
            """,
            (now, now, now, student["id"]),
        )
        elections = rows_to_dicts(cur.fetchall())
        for election in elections:
            election["period_lines"] = format_profile_election_period(
                election["start_time"],
                election["end_time"],
            )
    finally:
        conn.close()

    return render_template(
        "student_profile.html",
        student=student,
        elections=elections,
    )


# ---------------- THANKS ----------------
@app.route("/thanks")
def thanks():
    return render_template("thanks.html")


# ---------------- RESULTS ----------------
@app.route("/results")
def results():
    expire_elections()

    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM elections ORDER BY id DESC LIMIT 1")
    election = row_to_dict(cur.fetchone())

    if not election:
        conn.close()
        return render_template("results.html", status="no_election")

    if election["is_active"] == 1:
        conn.close()
        return render_template("results.html", status="running", election=election)

    cur.execute("""
        SELECT c.name, c.photo, COUNT(v.id) AS votes
        FROM candidates c
        LEFT JOIN votes v ON c.id=v.candidate_id
        WHERE c.election_id=?
        GROUP BY c.id
        ORDER BY votes DESC
    """, (election["id"],))

    results = rows_to_dicts(cur.fetchall())
    conn.close()

    return render_template(
        "results.html",
        status="ended",
        election=election,
        results=results,
        winner=results[0] if results else None
    )


# ---------------- ADMIN ----------------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST" and "admin_login" in request.form:
        password = request.form.get("password", "")
        if password.strip() and hmac.compare_digest(password, ADMIN_PASS):
            session.clear()
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("Wrong admin password", "danger")

    if "admin" not in session:
        return render_template("admin.html", logged_in=False)

    expire_elections()

    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if request.method == "POST" and "create_election" in request.form:
        try:
            duration = int(request.form["duration"])
        except (ValueError, KeyError):
            flash("Duration must be a number", "danger")
            duration = None

        if duration and duration > 0:
            start = datetime.now()
            end = start + timedelta(minutes=duration)

            cur.execute("UPDATE elections SET is_active=0")
            cur.execute(
                "INSERT INTO elections (name,is_active,start_time,end_time) VALUES (?,?,?,?)",
                (request.form["election_name"], 1, start.isoformat(), end.isoformat())
            )
            conn.commit()

    if request.method == "POST" and "add_candidate" in request.form:
        cur.execute("SELECT id FROM elections WHERE is_active=1 LIMIT 1")
        e = cur.fetchone()
        if e:
            photo = request.files.get("photo")
            if not photo or photo.filename == "":
                flash("Please choose a photo", "danger")
            elif not allowed_file(photo.filename):
                flash("Invalid image file type", "danger")
            else:
                try:
                    image = Image.open(photo)
                    image.verify()
                    photo.seek(0)
                except Exception:
                    flash("Invalid or corrupted image file", "danger")
                else:
                    filename = secure_filename(photo.filename)
                    # avoid collisions overwriting existing candidate photos
                    base, ext = os.path.splitext(filename)
                    candidate_name = request.form.get("candidate_name", "").strip()
                    if not candidate_name:
                        flash("Candidate name is required", "danger")
                    else:
                        unique_name = f"{base}_{int(datetime.now().timestamp())}{ext}"
                        photo.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
                        cur.execute(
                            "INSERT INTO candidates (election_id,name,photo) VALUES (?,?,?)",
                            (e["id"], candidate_name, unique_name)
                        )
                        conn.commit()

    if request.method == "POST" and "end_election" in request.form:
        cur.execute("UPDATE elections SET is_active=0 WHERE is_active=1")
        conn.commit()

    cur.execute("SELECT * FROM elections WHERE is_active=1 LIMIT 1")
    election = row_to_dict(cur.fetchone())

    cur.execute("SELECT COUNT(*) FROM students")
    total_students = cur.fetchone()[0]

    votes_cast = 0
    candidates = []

    if election:
        cur.execute("SELECT COUNT(*) FROM votes WHERE election_id=?", (election["id"],))
        votes_cast = cur.fetchone()[0]

        cur.execute("""
            SELECT c.id,c.name,c.photo,COUNT(v.id) AS votes
            FROM candidates c
            LEFT JOIN votes v ON c.id=v.candidate_id
            WHERE c.election_id=?
            GROUP BY c.id
            ORDER BY votes DESC
        """, (election["id"],))
        candidates = rows_to_dicts(cur.fetchall())

    cur.execute(
        """
        SELECT
            s.id,
            s.college_id,
            CASE WHEN v.student_id IS NOT NULL THEN 1 ELSE 0 END AS has_voted
        FROM students AS s
        LEFT JOIN votes AS v
          ON s.id = v.student_id AND v.election_id = ?
        ORDER BY s.college_id
        """,
        (election["id"] if election else None,),
    )
    students = rows_to_dicts(cur.fetchall())

    conn.close()

    return render_template(
        "admin.html",
        logged_in=True,
        election=election,
        candidates=candidates,
        students=students,
        total_students=total_students,
        votes_cast=votes_cast,
        votes_remaining=total_students - votes_cast,
        leader=candidates[0] if candidates else None
    )


# ---------------- DELETE CANDIDATE ----------------
@app.route("/delete_candidate/<int:cid>", methods=["POST"])
def delete_candidate(cid):
    if "admin" not in session:
        return redirect(url_for("admin"))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM votes WHERE candidate_id=?", (cid,))
    cur.execute("DELETE FROM candidates WHERE id=?", (cid,))
    conn.commit()
    conn.close()

    flash("Candidate deleted", "success")
    return redirect(url_for("admin"))


# ---------------- DELETE STUDENT ----------------
@app.route("/delete_student/<college_id>", methods=["POST"])
def delete_student(college_id):
    if "admin" not in session:
        return redirect(url_for("admin"))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM students WHERE college_id=?", (college_id,))
    row = cur.fetchone()

    if row:
        cur.execute("DELETE FROM votes WHERE student_id=?", (row[0],))
        cur.execute("DELETE FROM students WHERE id=?", (row[0],))
        conn.commit()
        flash(f"Student {college_id} deleted", "success")

    conn.close()
    return redirect(url_for("admin"))



@app.route("/robots.txt")
def robots_txt():
    content = """User-agent: *
Allow: /

Sitemap: https://aman5.pythonanywhere.com/sitemap.xml
"""
    return app.response_class(content, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    content = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url>
            <loc>https://aman5.pythonanywhere.com/</loc>
        </url>
    </urlset>
    """
    return app.response_class(content, mimetype="application/xml")


# ---------------- 404 ERROR ----------------
@app.errorhandler(404)
def page_not_found(error):
    return render_template("404.html"), 404


@app.route("/googlec246928b39a849e4.html")
def google_verification():
    return "google-site-verification: googlec246928b39a849e4.html"





# ---------------- RUN ----------------
if __name__ == "__main__":
    init_db()
    seed_students()
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode)
