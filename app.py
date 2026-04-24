import json
import os
import re
import shlex
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

import eventlet
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_socketio import SocketIO, emit, join_room
from werkzeug.utils import secure_filename

from models import TestRun, User, db


eventlet.monkey_patch()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
REPORTS_DIR = BASE_DIR / "reports"
LIGHTHOUSE_DIR = BASE_DIR / "lighthouse_reports"
ALLOWED_EXTENSIONS = {"jmx"}

for directory in (UPLOAD_DIR, REPORTS_DIR, LIGHTHOUSE_DIR):
    directory.mkdir(parents=True, exist_ok=True)


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")


db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id: str):
    return User.query.get(int(user_id))


def is_safe_child(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def append_and_emit_log(test_id: int, line: str) -> None:
    with app.app_context():
        test_run = TestRun.query.get(test_id)
        if not test_run:
            return
        clean = line.rstrip("\n")
        if clean:
            test_run.append_log(clean)
            db.session.commit()
            socketio.emit("log_output", {"line": clean}, room=str(test_id), namespace="/test")


def run_jmeter_test(test_id: int, jmx_file_path: str) -> None:
    with app.app_context():
        test_run = TestRun.query.get(test_id)
        if not test_run:
            return
        test_run.status = "running"
        db.session.commit()

    result_file = REPORTS_DIR / f"results_{test_id}_{uuid.uuid4().hex}.jtl"
    report_dir = REPORTS_DIR / f"report_{test_id}_{uuid.uuid4().hex}"
    report_dir.mkdir(parents=True, exist_ok=False)

    cmd = [
        "jmeter",
        "-n",
        "-t",
        jmx_file_path,
        "-l",
        str(result_file),
        "-e",
        "-o",
        str(report_dir),
    ]

    append_and_emit_log(test_id, f"Running command: {' '.join(shlex.quote(x) for x in cmd)}")

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        if process.stdout:
            for line in process.stdout:
                append_and_emit_log(test_id, line)

        return_code = process.wait()
        with app.app_context():
            test_run = TestRun.query.get(test_id)
            if not test_run:
                return
            if return_code == 0:
                test_run.status = "completed"
                test_run.report_path = str(report_dir.relative_to(BASE_DIR))
                db.session.commit()
                socketio.emit(
                    "test_complete",
                    {
                        "test_id": test_id,
                        "status": "completed",
                        "report_url": url_for("view_report", test_id=test_id),
                    },
                    room=str(test_id),
                    namespace="/test",
                )
            else:
                test_run.status = "failed"
                db.session.commit()
                socketio.emit(
                    "test_complete",
                    {"test_id": test_id, "status": "failed"},
                    room=str(test_id),
                    namespace="/test",
                )
    except FileNotFoundError:
        append_and_emit_log(test_id, "ERROR: JMeter not found. Ensure 'jmeter' is installed and in PATH.")
        with app.app_context():
            test_run = TestRun.query.get(test_id)
            if test_run:
                test_run.status = "failed"
                db.session.commit()
    except Exception as exc:  # noqa: BLE001
        append_and_emit_log(test_id, f"ERROR: Unexpected failure: {exc}")
        with app.app_context():
            test_run = TestRun.query.get(test_id)
            if test_run:
                test_run.status = "failed"
                db.session.commit()


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if len(username) < 3 or len(password) < 6:
            flash("Username must be 3+ chars and password must be 6+ chars.", "error")
            return render_template("register.html")

        existing = User.query.filter_by(username=username).first()
        if existing:
            flash("Username already exists.", "error")
            return render_template("register.html")

        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash("Invalid username or password.", "error")
            return render_template("login.html")

        login_user(user)
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    tests = TestRun.query.filter_by(user_id=current_user.id).order_by(TestRun.created_at.desc()).all()
    return render_template("dashboard.html", tests=tests)


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        if "jmx_file" not in request.files:
            flash("No file part in request.", "error")
            return redirect(url_for("upload"))

        file = request.files["jmx_file"]
        if file.filename == "":
            flash("No file selected.", "error")
            return redirect(url_for("upload"))

        if not allowed_file(file.filename):
            flash("Only .jmx files are allowed.", "error")
            return redirect(url_for("upload"))

        original_filename = file.filename
        safe_name = secure_filename(original_filename)
        unique_name = f"{uuid.uuid4().hex}_{safe_name}"
        destination = UPLOAD_DIR / unique_name
        file.save(destination)

        test_run = TestRun(
            user_id=current_user.id,
            filename=unique_name,
            original_filename=original_filename,
            status="queued",
            created_at=datetime.utcnow(),
        )
        db.session.add(test_run)
        db.session.commit()

        worker = Thread(target=run_jmeter_test, args=(test_run.id, str(destination)), daemon=True)
        worker.start()

        flash("Test queued successfully.", "success")
        return redirect(url_for("test_status", test_id=test_run.id))

    return render_template("upload.html")


@app.route("/test/<int:test_id>")
@login_required
def test_status(test_id: int):
    test_run = TestRun.query.filter_by(id=test_id, user_id=current_user.id).first_or_404()
    return render_template("test_status.html", test=test_run)


@app.route("/report/<int:test_id>")
@login_required
def view_report(test_id: int):
    test_run = TestRun.query.filter_by(id=test_id, user_id=current_user.id).first_or_404()
    if test_run.status != "completed" or not test_run.report_path:
        flash("Report is not available yet.", "error")
        return redirect(url_for("test_status", test_id=test_id))
    return render_template("view_report.html", test=test_run)


@app.route("/report/<int:test_id>/index")
@login_required
def serve_report_index(test_id: int):
    test_run = TestRun.query.filter_by(id=test_id, user_id=current_user.id).first_or_404()
    if not test_run.report_path:
        abort(404)
    report_dir = BASE_DIR / test_run.report_path
    if not is_safe_child(BASE_DIR, report_dir):
        abort(400)
    index_file = report_dir / "index.html"
    if not index_file.exists():
        abort(404)
    return send_file(index_file)


@app.route("/report/<int:test_id>/assets/<path:subpath>")
@login_required
def serve_report_assets(test_id: int, subpath: str):
    test_run = TestRun.query.filter_by(id=test_id, user_id=current_user.id).first_or_404()
    if not test_run.report_path:
        abort(404)

    report_dir = BASE_DIR / test_run.report_path
    requested = report_dir / subpath

    if not is_safe_child(report_dir, requested):
        abort(400)
    if not requested.exists() or not requested.is_file():
        abort(404)
    return send_file(requested)


@app.route("/lighthouse", methods=["GET", "POST"])
@login_required
def lighthouse_audit():
    latest_report = None
    score = None

    if request.method == "POST":
        url = request.form.get("url", "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            flash("Enter a valid URL starting with http:// or https://", "error")
            return render_template("lighthouse.html", latest_report=latest_report, score=score)

        report_id = uuid.uuid4().hex
        html_path = LIGHTHOUSE_DIR / f"lighthouse_{report_id}.html"
        json_path = LIGHTHOUSE_DIR / f"lighthouse_{report_id}.json"

        cmd = [
            "lighthouse",
            url,
            "--output",
            "html",
            "--output",
            "json",
            "--output-path",
            str(LIGHTHOUSE_DIR / f"lighthouse_{report_id}"),
            '--chrome-flags=--headless',
            "--quiet",
        ]

        try:
            process = subprocess.run(cmd, check=True, capture_output=True, text=True)
            if process.stderr:
                flash(process.stderr[:500], "warning")

            if json_path.exists():
                with json_path.open("r", encoding="utf-8") as fp:
                    data = json.load(fp)
                perf_score = data.get("categories", {}).get("performance", {}).get("score")
                if perf_score is not None:
                    score = int(perf_score * 100)

            latest_report = html_path.name
            flash("Lighthouse audit completed.", "success")
        except FileNotFoundError:
            flash("Lighthouse is not installed. Install with: npm i -g lighthouse", "error")
        except subprocess.CalledProcessError as exc:
            flash(f"Lighthouse failed: {(exc.stderr or exc.stdout)[:600]}", "error")

    return render_template("lighthouse.html", latest_report=latest_report, score=score)


@app.route("/lighthouse_reports/<path:filename>")
@login_required
def serve_lighthouse_report(filename: str):
    safe_filename = secure_filename(filename)
    if filename != safe_filename:
        abort(400)
    file_path = LIGHTHOUSE_DIR / safe_filename
    if not is_safe_child(LIGHTHOUSE_DIR, file_path) or not file_path.exists():
        abort(404)
    return send_file(file_path)


@app.route("/tools")
@login_required
def tools_showcase():
    tools = [
        {
            "name": "Apache JMeter",
            "description": "Open-source load testing powerhouse for APIs, web apps, and distributed workloads.",
            "tag": "Load Testing",
        },
        {
            "name": "LoadRunner",
            "description": "Enterprise-grade protocol coverage and deep analysis for complex performance testing.",
            "tag": "Enterprise",
        },
        {
            "name": "BlazeMeter",
            "description": "Cloud-native continuous testing platform with JMeter compatibility and CI/CD integration.",
            "tag": "Cloud",
        },
        {
            "name": "k6",
            "description": "Developer-first, scriptable performance testing with JavaScript and modern observability.",
            "tag": "Developer",
        },
        {
            "name": "Gatling",
            "description": "High-performance Scala-based load testing with detailed reports and simulations.",
            "tag": "Simulation",
        },
        {
            "name": "Lighthouse",
            "description": "Web quality auditing tool for performance, accessibility, and best-practices insights.",
            "tag": "Web Audit",
        },
    ]
    return render_template("tools.html", tools=tools)


@socketio.on("join_room", namespace="/test")
def socket_join(data):
    test_id = str(data.get("test_id", "")).strip()
    if not re.fullmatch(r"\d+", test_id):
        emit("log_output", {"line": "Invalid room."})
        return
    join_room(test_id)
    test_run = TestRun.query.get(int(test_id))
    if test_run and test_run.log:
        for line in test_run.log.strip().splitlines():
            emit("log_output", {"line": line})
    if test_run and test_run.status in {"completed", "failed"}:
        emit("test_complete", {"test_id": int(test_id), "status": test_run.status})


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
