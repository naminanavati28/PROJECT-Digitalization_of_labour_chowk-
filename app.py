from datetime import date, datetime, timedelta
import os
import random
import tempfile
import time
import urllib.request
from flask import Flask, render_template, request, redirect, session, flash, jsonify  # type: ignore
from flask_sqlalchemy import SQLAlchemy  # type: ignore
import re
from werkzeug.utils import secure_filename
import cv2
import pytesseract  # type: ignore
from PIL import Image
import firebase_admin
from firebase_admin import credentials, storage


try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except ImportError:
    pass  # python-dotenv optional; use env vars directly

# Tesseract path (Windows)
if os.name == "nt":
    pytesseract.pytesseract.tesseract_cmd = (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    )

HELPLINE_NUMBER = "99999XXXXX"
USE_FIREBASE = bool(os.getenv("FIREBASE_CREDENTIALS_PATH"))
_firebase_admin_initialized = False


def validate_indian_phone(phone):
    """Validate Indian mobile: 10 digits, starts with 6/7/8/9."""
    if not phone:
        return False
    p = re.sub(r"\D", "", str(phone))
    if len(p) == 10:
        return p[0] in "6789"
    if len(p) == 12 and p.startswith("91"):
        return p[2] in "6789"
    return False


def normalize_phone(phone):
    """Return 10-digit Indian phone for storage/lookup."""
    p = re.sub(r"\D", "", str(phone))
    if len(p) == 12 and p.startswith("91"):
        return p[2:]
    if len(p) == 10:
        return p
    return phone


def send_sms_otp(phone, otp):
    """Send OTP via Twilio SMS. Returns True if sent, False otherwise (falls back to terminal)."""
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_num = os.getenv("TWILIO_PHONE_NUMBER")
    if not all([sid, token, from_num]):
        return False
    to_num = normalize_phone(phone)
    if len(to_num) != 10:
        return False
    to_num = "+91" + to_num
    try:
        from twilio.rest import Client  # type: ignore

        client = Client(sid, token)
        client.messages.create(
            body=f"Your Kaam Bharat OTP is: {otp}. Valid for 5 minutes.",
            from_=from_num,
            to=to_num,
        )
        return True
    except Exception as e:
        print("SMS send error:", e)
        return False


def to_ampm(time_str):
    return datetime.strptime(time_str, "%H:%M").strftime("%I:%M %p")


def detect_shift(start_time):
    hour = int(start_time.split(":")[0])
    if 6 <= hour < 18:
        return "Day"
    else:
        return "Night"


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "secret123-change-in-production")

# PostgreSQL (architecture flow) or SQLite fallback for local dev
database_url = os.getenv("DATABASE_URL")
if database_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///labour.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

GOOGLE_MAPS_API_KEY = "https://maps.googleapis.com/maps/api/js?key=AIzaSyAOVYRIgupAurZup5y1PRh8Ismb1A3lLao&libraries=places&callback=initMap"
GOOGLE_MAPS_KEY = "AIzaSyAOVYRIgupAurZup5y1PRh8Ismb1A3lLao"  # For script src


def init_firebase_admin():
    """Initialize Firebase Admin SDK for token verification and Storage."""
    global _firebase_admin_initialized
    if _firebase_admin_initialized:
        return
    cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
    if cred_path and os.path.exists(cred_path):
        try:
            import firebase_admin  # type: ignore
            from firebase_admin import credentials  # type: ignore

            cred = credentials.Certificate(cred_path)
            opts = {}
            bucket = os.getenv("FIREBASE_STORAGE_BUCKET")
            if bucket:
                opts["storageBucket"] = bucket.replace("gs://", "")
            firebase_admin.initialize_app(cred, opts)
            _firebase_admin_initialized = True
        except ImportError as e:
            print(
                "Warning: firebase-admin not installed. Run: pip install firebase-admin"
            )


@app.context_processor
def inject_maps_key():
    ctx = {
        "GOOGLE_MAPS_API_KEY": GOOGLE_MAPS_API_KEY,
        "GOOGLE_MAPS_KEY": GOOGLE_MAPS_KEY,
        "HELPLINE_NUMBER": HELPLINE_NUMBER,
    }
    if session.get("user_id") and session.get("name"):
        ctx["nav_user_name"] = f"Hello, {session['name']}"
    else:
        ctx["nav_user_name"] = None
    return ctx


@app.template_filter("to_ampm")
def template_to_ampm(time_str):
    """Format 24h time to 12h AM/PM for templates."""
    if not time_str:
        return ""
    try:
        return datetime.strptime(str(time_str), "%H:%M").strftime("%I:%M %p")
    except (ValueError, TypeError):
        return time_str


@app.template_filter("aadhaar_src")
def aadhaar_src(url_or_path):
    """Return full URL/path for Aadhaar image (Firebase URL or local static path)."""
    if not url_or_path:
        return ""
    if url_or_path.startswith("http"):
        return url_or_path
    return f"/static/aadhaar/{url_or_path}"


def get_firebase_client_config():
    """Firebase client config for frontend (safe to expose)."""
    api_key = os.getenv("FIREBASE_API_KEY")
    if not api_key:
        return None
    return {
        "apiKey": api_key,
        "authDomain": os.getenv("FIREBASE_AUTH_DOMAIN", ""),
        "projectId": os.getenv("FIREBASE_PROJECT_ID", ""),
        "storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET", ""),
        "messagingSenderId": os.getenv("FIREBASE_MESSAGING_SENDER_ID", ""),
        "appId": os.getenv("FIREBASE_APP_ID", ""),
    }


# ==============================
# AADHAAR HELPERS
# ==============================


def extract_text(image_path):
    return pytesseract.image_to_string(Image.open(image_path))


def valid_aadhaar_text(text):
    aadhaar_pattern = r"\b\d{4}\s\d{4}\s\d{4}\b"
    keywords = ["government of india", "dob", "year of birth", "male", "female"]
    t = text.lower()
    return bool(re.search(aadhaar_pattern, text)) and any(k in t for k in keywords)


def has_face(image_path):
    face = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    img = cv2.imread(image_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return len(face.detectMultiScale(gray, 1.1, 4)) > 0


def verify_aadhaar_image(image_path):
    """
    Combines existing Aadhaar helpers (local file path)
    """
    try:
        text = extract_text(image_path)
        if not valid_aadhaar_text(text):

            return False, "Invalid Aadhaar text"
        if not has_face(image_path):
            return False, "Face not detected on Aadhaar"
        return True, "Aadhaar verified"
    except Exception as e:
        print("AADHAAR ERROR:", e)
        return False, "Aadhaar verification error"


def verify_aadhaar_from_url(url):
    """
    Download Aadhaar image from Firebase Storage URL and verify.
    Returns (success: bool, message: str)
    """
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            urllib.request.urlretrieve(url, tmp.name)
            ok, msg = verify_aadhaar_image(tmp.name)
            os.unlink(tmp.name)
            return ok, msg
    except Exception as e:
        print("AADHAAR URL ERROR:", e)
        return False, "Could not verify Aadhaar image"


# ======================================================
# DATABASE MODELS
# ======================================================


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    firebase_uid = db.Column(
        db.String(128), unique=True, nullable=True
    )  # Firebase Auth

    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(15), unique=True, nullable=False)

    aadhaar_image = db.Column(
        db.String(500), nullable=False
    )  # Firebase Storage URL or filename

    role = db.Column(db.String(20))  # worker / manager
    is_verified = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=db.func.now())

    def __repr__(self):
        return f"<User {self.phone}>"


class WorkerProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True)

    avg_rating = db.Column(db.Float, default=0.0)
    completed_jobs = db.Column(db.Integer, default=0)

    def __repr__(self):
        return f"<WorkerProfile {self.user_id}>"


class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), nullable=False)

    wage = db.Column(db.Integer, nullable=False)
    location = db.Column(db.String(100), nullable=False)

    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)

    work_date = db.Column(db.Date, nullable=False)
    duration_days = db.Column(db.Integer, nullable=False)

    start_time = db.Column(db.String(20), nullable=False)
    end_time = db.Column(db.String(20), nullable=False)

    required_workers = db.Column(db.Integer, nullable=False)

    food = db.Column(db.Boolean, default=False)
    stay = db.Column(db.Boolean, default=False)
    transport = db.Column(db.Boolean, default=False)
    esi_pf = db.Column(db.Boolean, default=False)
    manager_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    created_at = db.Column(db.DateTime, default=db.func.now())


class Application(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    worker_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey("job.id"), nullable=False)

    status = db.Column(db.String(30), default="pending")
    # pending | accepted | worker_confirmed | rejected | completed

    # ✅ NEW FIELDS
    feedback = db.Column(db.Text)
    rating = db.Column(db.Integer)

    job = db.relationship("Job", backref="applications")
    worker = db.relationship("User")

    completed_at = db.Column(db.DateTime)


with app.app_context():
    db.create_all()
    # Add firebase_uid column if missing (migration for existing DBs)
    try:
        from sqlalchemy import text

        with db.engine.connect() as conn:
            conn.execute(text("SELECT firebase_uid FROM user LIMIT 1"))
            conn.commit()
    except Exception:
        try:
            with db.engine.connect() as conn:
                if "sqlite" in str(db.engine.url):
                    conn.execute(
                        text("ALTER TABLE user ADD COLUMN firebase_uid VARCHAR(128)")
                    )
                else:
                    conn.execute(
                        text(
                            'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS firebase_uid VARCHAR(128)'
                        )
                    )
                conn.commit()
        except Exception as e:
            print("Migration note:", e)


class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    job_id = db.Column(db.Integer, db.ForeignKey("job.id"), nullable=False)
    reporter_role = db.Column(db.String(20), nullable=False)  # "manager"
    reported_user_id = db.Column(db.Integer, nullable=False)  # worker_id

    reason = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=db.func.now())


# ======================================================
# LANDING / LOGIN PAGE (GET)
# ======================================================
@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    use_firebase = bool(get_firebase_client_config()) and "legacy" not in request.args
    # Form POST = legacy flow (Firebase uses /api/auth/login via JS)
    if request.method == "POST" and request.form:
        phone = request.form.get("phone", "").strip()
        if phone:
            if not validate_indian_phone(phone):
                flash(
                    "Invalid phone number. Enter 10-digit Indian mobile (e.g. 9876543210).",
                    "error",
                )
            else:
                phone = normalize_phone(phone)
                user = User.query.filter_by(phone=phone).first()
                if not user or not user.is_verified:
                    return redirect("/register")
                otp = str(random.randint(1000, 9999))
                session["otp"] = otp
                session["otp_time"] = time.time()
                session["otp_attempts"] = 0
                session["otp_last_sent"] = time.time()
                session["auth_flow"] = "login"
                session["temp_user"] = {"phone": phone}
                if send_sms_otp(phone, otp):
                    flash("OTP sent to your phone.", "success")
                else:
                    print("LOGIN OTP (no SMS config):", otp)
                return redirect("/verify-otp")

    return render_template("login.html", use_firebase=use_firebase)


# ======================================================
# REGISTER PAGE (GET & POST)
# ======================================================
@app.route("/register", methods=["GET", "POST"])
def register():
    use_firebase = bool(get_firebase_client_config()) and "legacy" not in request.args
    # Form POST = legacy flow (Firebase uses /api/auth/register via JS)
    if request.method == "POST" and (request.form or request.files):
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        role = request.form.get("role")
        aadhaar = request.files.get("aadhaar")

        if not all([name, phone, role, aadhaar]):
            flash("Please fill all fields.", "error")
            return render_template("register.html", use_firebase=use_firebase)

        if not validate_indian_phone(phone):
            flash(
                "Invalid phone number. Enter 10-digit Indian mobile (e.g. 9876543210).",
                "error",
            )
            return render_template("register.html", use_firebase=use_firebase)

        phone = normalize_phone(phone)
        filename = secure_filename(f"{phone}_aadhaar.jpg")
        upload_path = os.path.join("static", "aadhaar", filename)
        os.makedirs(os.path.dirname(upload_path), exist_ok=True)
        aadhaar.save(upload_path)
        is_verified, message = verify_aadhaar_image(upload_path)

        if not is_verified:
            flash(f"Aadhaar verification failed: {message}", "error")
            return render_template("register.html", use_firebase=use_firebase)

        otp = str(random.randint(1000, 9999))
        session["otp"] = otp
        session["otp_time"] = time.time()
        session["otp_attempts"] = 0
        session["otp_last_sent"] = time.time()
        session["auth_flow"] = "register"
        session["temp_user"] = {
            "name": name,
            "phone": phone,
            "role": role,
            "aadhaar_image": filename,
        }
        if send_sms_otp(phone, otp):
            flash("OTP sent to your phone.", "success")
        else:
            print("REGISTER OTP (no SMS config):", otp)
        return redirect("/verify-otp")

    return render_template("register.html", use_firebase=use_firebase)


# ======================================================
# VERIFY OTP
# ======================================================
@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    if "otp" not in session or "temp_user" not in session or "auth_flow" not in session:
        return redirect("/login")

    error = None

    if request.method == "POST":
        entered_otp = request.form.get("otp")

        if time.time() - session.get("otp_time", 0) > 300:
            error = "OTP expired"

        elif session.get("otp_attempts", 0) >= 3:
            error = "Too many attempts"

        elif entered_otp != session["otp"]:
            session["otp_attempts"] += 1
            error = "Invalid OTP"

        else:
            flow = session["auth_flow"]
            temp = session["temp_user"]

            # 🔐 LOGIN FLOW
            if flow == "login":
                user = User.query.filter_by(phone=temp["phone"]).first()

                if not user or not user.is_verified:
                    session.clear()
                    return redirect("/register")

                session.clear()
                session["user_id"] = user.id
                session["name"] = user.name
                session["role"] = user.role
                return redirect("/dashboard")

            # 🆕 REGISTER FLOW
            elif flow == "register":
                user = User(
                    name=temp["name"],
                    phone=temp["phone"],
                    role=temp["role"],
                    aadhaar_image=temp["aadhaar_image"],
                    is_verified=True,
                )
                db.session.add(user)
                db.session.commit()

                session.clear()
                session["user_id"] = user.id
                session["name"] = user.name
                session["role"] = user.role
                return redirect("/dashboard")

    return render_template("verify_otp.html", error=error)


# ======================================================
# RESEND OTP
# ======================================================


@app.route("/resend-otp")
def resend_otp():
    if "temp_user" not in session:
        return redirect("/login")

    if time.time() - session.get("otp_last_sent", 0) < 30:
        flash("Please wait 30 seconds before resending.", "warning")
        return redirect("/verify-otp")

    otp = str(random.randint(1000, 9999))
    session["otp"] = otp
    session["otp_time"] = time.time()
    session["otp_attempts"] = 0
    session["otp_last_sent"] = time.time()

    phone = session["temp_user"].get("phone", "")
    if send_sms_otp(phone, otp):
        flash("OTP resent to your phone.", "success")
    else:
        print("RESENT OTP (no SMS config):", otp)
    return redirect("/verify-otp")


# ======================================================
# FIREBASE AUTH API (Architecture Flow)
# ======================================================
@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    """Verify Firebase ID token and create session for login."""
    data = request.get_json() or {}
    id_token = data.get("idToken") or data.get("id_token")
    if not id_token:
        return jsonify({"ok": False, "error": "Missing idToken"}), 400

    try:
        init_firebase_admin()
        from firebase_admin import auth as firebase_auth

        decoded = firebase_auth.verify_id_token(id_token)
        firebase_uid = decoded["uid"]
        phone = decoded.get("phone_number", "").replace("+91", "").strip()
        if not phone and "phone_number" in decoded:
            phone = (
                decoded["phone_number"].replace("+", "").replace("91", "", 1).strip()
            )

        user = User.query.filter_by(phone=phone).first()
        if not user or not user.is_verified:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "User not registered. Please register first.",
                    }
                ),
                404,
            )

        user.firebase_uid = firebase_uid
        db.session.commit()

        session.clear()
        session["user_id"] = user.id
        session["name"] = user.name
        session["role"] = user.role
        return jsonify({"ok": True, "redirect": "/dashboard"})
    except Exception as e:
        print("Firebase auth login error:", e)
        return jsonify({"ok": False, "error": "Authentication failed"}), 401


@app.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    """Verify Firebase token and create new user with Aadhaar from Firebase Storage."""
    data = request.get_json() or {}
    id_token = data.get("idToken") or data.get("id_token")
    name = data.get("name")
    phone = data.get("phone", "").strip()
    role = data.get("role")
    aadhaar_url = data.get("aadhaar_url") or data.get("aadhaarUrl")

    if not all([id_token, name, phone, role]):
        return jsonify({"ok": False, "error": "Missing required fields"}), 400

    if not aadhaar_url:
        return jsonify({"ok": False, "error": "Aadhaar image is required"}), 400

    try:
        init_firebase_admin()
        from firebase_admin import auth as firebase_auth

        decoded = firebase_auth.verify_id_token(id_token)
        firebase_uid = decoded["uid"]

        if User.query.filter_by(phone=phone).first():
            return (
                jsonify(
                    {"ok": False, "error": "Phone already registered. Please login."}
                ),
                409,
            )

        is_verified, message = verify_aadhaar_from_url(aadhaar_url)
        if not is_verified:
            return (
                jsonify(
                    {"ok": False, "error": f"Aadhaar verification failed: {message}"}
                ),
                400,
            )

        user = User(
            firebase_uid=firebase_uid,
            name=name,
            phone=phone,
            role=role,
            aadhaar_image=aadhaar_url,
            is_verified=True,
        )
        db.session.add(user)
        db.session.commit()

        session.clear()
        session["user_id"] = user.id
        session["name"] = user.name
        session["role"] = user.role
        return jsonify({"ok": True, "redirect": "/dashboard"})
    except Exception as e:
        print("Firebase auth register error:", e)
        return jsonify({"ok": False, "error": str(e) or "Registration failed"}), 500


@app.route("/api/firebase-config")
def api_firebase_config():
    """Return Firebase client config for frontend."""
    config = get_firebase_client_config()
    if not config:
        return jsonify({"ok": False, "error": "Firebase not configured"}), 503
    return jsonify({"ok": True, "config": config})


@app.route("/api/upload-aadhaar", methods=["POST"])
def api_upload_aadhaar():
    """Upload Aadhaar image to Firebase Storage or local fallback."""
    file = request.files.get("aadhaar") or request.files.get("file")
    phone = request.form.get("phone", "unknown")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "No file provided"}), 400

    filename = secure_filename(f"{phone}_{int(time.time())}_aadhaar.jpg")

    try:
        init_firebase_admin()
        from firebase_admin import storage

        bucket = storage.bucket()
        blob = bucket.blob(f"aadhaar/{filename}")
        blob.upload_from_file(file, content_type=file.content_type or "image/jpeg")
        blob.make_public()
        url = blob.public_url
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        print("Firebase Storage upload error:", e)
        # Fallback to local storage
        try:
            upload_dir = os.path.join("static", "aadhaar")
            os.makedirs(upload_dir, exist_ok=True)
            local_path = os.path.join(upload_dir, filename)
            file.save(local_path)
            # Return absolute URL for verification
            base_url = request.url_root.rstrip("/")
            url = f"{base_url}/static/aadhaar/{filename}"
            return jsonify({"ok": True, "url": url})
        except Exception as e2:
            print("Local upload error:", e2)
            return jsonify({"ok": False, "error": "Upload failed"}), 500


# ======================================================
# DASHBOARD
# ======================================================


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session or "role" not in session:
        return redirect("/")

    jobs = []  # ✅ REQUIRED: prevents UnboundLocalError

    # ==================================================
    # MANAGER DASHBOARD
    # ==================================================
    if session["role"] == "manager":

        jobs = (
            Job.query.filter_by(manager_id=session["user_id"])
            .order_by(
                Job.work_date.asc(),
                Job.created_at.desc(),
            )
            .all()
        )

        for job in jobs:
            job.start_time_ampm = to_ampm(job.start_time)
            job.end_time_ampm = to_ampm(job.end_time)
            job.shift_type = detect_shift(job.start_time)

        job_capacity = {}
        for job in jobs:
            confirmed = Application.query.filter_by(
                job_id=job.id, status="worker_confirmed"
            ).count()
            job_capacity[job.id] = confirmed

        return render_template(
            "manager_dashboard.html",
            jobs=jobs,
            manager_name=session["name"],
            job_capacity=job_capacity,
        )
    # assert isinstance(job_capacity, dict)
    # ==================================================
    # WORKER DASHBOARD
    # ==================================================

    query = Job.query

    # -------- FILTERS --------
    min_wage = request.args.get("wage", type=int)
    location = request.args.get("location")
    category = request.args.get("category")
    time_pref = request.args.get("time")

    if min_wage:
        query = query.filter(Job.wage >= min_wage)

    if location:
        query = query.filter(Job.location.ilike(f"%{location}%"))

    if category:
        query = query.filter(Job.category == category)

    if time_pref == "Day":
        query = query.filter(Job.start_time >= "06:00", Job.start_time < "18:00")
    elif time_pref == "Night":
        query = query.filter((Job.start_time < "06:00") | (Job.start_time >= "18:00"))

    jobs = query.order_by(Job.work_date.asc()).all()

    # ==================================================
    # WORKER APPLICATIONS
    # ==================================================
    applications = Application.query.filter_by(worker_id=session["user_id"]).all()

    application_map = {app.job_id: app for app in applications}

    # ==================================================
    # JOB CAPACITY (MUST COME FIRST ❗)
    # ==================================================
    job_capacity = {}
    for job in jobs:
        confirmed = Application.query.filter_by(
            job_id=job.id, status="worker_confirmed"
        ).count()
        job_capacity[job.id] = confirmed

    # ==================================================
    # LOCKED DATES (worker already hired)
    # ==================================================
    locked_dates = set(
        job.work_date
        for job in db.session.query(Job)
        .join(Application, Application.job_id == Job.id)
        .filter(
            Application.worker_id == session["user_id"],
            Application.status == "worker_confirmed",
        )
        .all()
    )

    # ==================================================
    # BUILD FINAL JOB LIST
    # ==================================================
    filtered_jobs = []

    for job in jobs:
        app = application_map.get(job.id)

        if app:
            job.application_status = app.status
            job.application_id = app.id

            # ❌ hide completed jobs
            if app.status == "completed":
                continue
        else:
            job.application_status = "not_applied"
            job.application_id = None

        # 🔒 Date lock
        job.date_locked = job.work_date in locked_dates

        # 🔒 Capacity lock
        job.capacity_full = job_capacity.get(job.id, 0) >= job.required_workers

        # -------- TIME FORMAT --------
        job.start_time_ampm = to_ampm(job.start_time)
        job.end_time_ampm = to_ampm(job.end_time)
        job.shift_type = detect_shift(job.start_time)

        filtered_jobs.append(job)

    # ==================================================
    # SORT: INTERESTED / ACCEPTED ON TOP
    # ==================================================
    def job_priority(job):
        if job.application_status == "pending":
            return 0
        if job.application_status in ["accepted", "worker_confirmed"]:
            return 1
        return 2

    jobs = sorted(filtered_jobs, key=job_priority)

    # ==================================================
    # RENDER
    # ==================================================
    return render_template(
        "worker_dashboard.html",
        jobs=jobs,
        job_capacity=job_capacity,
        worker_name=session["name"],
    )


# ======================================================
# POST JOB (MANAGER)
# ======================================================


@app.route("/post-job", methods=["GET", "POST"])
def post_job():
    # 🔐 Manager only
    if session.get("role") != "manager":
        return redirect("/dashboard")

    # =====================
    # POST (CREATE JOB)
    # =====================
    if request.method == "POST":

        # ---------- DATE VALIDATION ----------
        work_date_str = request.form.get("work_date")
        if not work_date_str:
            return redirect("/post-job")

        work_date = date.fromisoformat(work_date_str)
        if work_date < date.today():
            return redirect("/post-job")

        # ---------- TIME VALIDATION ----------
        start_time = request.form.get("start_time")
        end_time = request.form.get("end_time")

        if not start_time or not end_time:
            return redirect("/post-job")

        start_dt = datetime.strptime(start_time, "%H:%M")
        end_dt = datetime.strptime(end_time, "%H:%M")

        # ❌ End time must be AFTER start time
        if end_dt <= start_dt:
            return redirect("/post-job")

        # ---------- DURATION VALIDATION ----------
        duration_days = int(request.form.get("duration_days", 0))
        if duration_days < 1:
            return redirect("/post-job")

        # ---------- REQUIRED WORKERS ----------
        required_workers = int(request.form.get("required_workers", 1))
        if required_workers < 1:
            return redirect("/post-job")

        # ---------- WAGE ----------
        wage = int(request.form.get("wage", 0))
        if wage < 0:
            wage = 0

        latitude = float(request.form.get("latitude"))
        longitude = float(request.form.get("longitude"))

        job = Job(
            title=request.form.get("title"),
            category=request.form.get("category"),
            wage=wage,
            location=request.form.get("location"),
            latitude=latitude,
            longitude=longitude,
            work_date=work_date,  # ✅ matches model
            duration_days=duration_days,
            start_time=start_time,
            end_time=end_time,
            required_workers=required_workers,  # ✅ matches model
            food="food" in request.form,
            stay="stay" in request.form,
            transport="transport" in request.form,
            esi_pf="esi_pf" in request.form,
            manager_id=session["user_id"],
        )

        db.session.add(job)
        db.session.commit()

        return redirect("/dashboard")

    # =====================
    # GET (SHOW FORM)
    # =====================
    today = date.today().isoformat()
    return render_template("post_job.html", today=today)


# ======================================================
# APPLY (WORKER)  ✅ FIXED ROUTE
# ======================================================


@app.route("/apply/<int:job_id>")
def apply(job_id):
    if session.get("role") != "worker":
        return redirect("/dashboard")

    # prevent duplicate application
    existing = Application.query.filter_by(
        worker_id=session["user_id"], job_id=job_id
    ).first()

    if existing:
        return redirect("/dashboard")

    app = Application(worker_id=session["user_id"], job_id=job_id, status="pending")

    db.session.add(app)
    db.session.commit()

    return redirect("/dashboard")


# ======================================================
# EXPRESS INTEREST (WORKER) - NEW ROUTE
# ======================================================
@app.route("/job/<int:job_id>/interest")
def worker_interest_job(job_id):
    if session.get("role") != "worker":
        return redirect("/dashboard")

    job = Job.query.get_or_404(job_id)

    # ==================================================
    # 1️⃣ HARD CAPACITY CHECK (accepted + confirmed)
    # ==================================================
    occupied_count = Application.query.filter(
        Application.job_id == job.id,
        Application.status.in_(["accepted", "worker_confirmed"]),
    ).count()

    if occupied_count >= job.required_workers:
        flash("This job is already full.", "error")
        return redirect("/dashboard")

    # ==================================================
    # 2️⃣ DATE LOCK CHECK (already hired on same date)
    # ==================================================
    date_locked = (
        db.session.query(Application)
        .join(Job, Job.id == Application.job_id)
        .filter(
            Application.worker_id == session["user_id"],
            Application.status.in_(["accepted", "worker_confirmed"]),
            Job.work_date == job.work_date,
        )
        .first()
    )

    if date_locked:
        flash("You are already hired for another job on this date.", "error")
        return redirect("/dashboard")

    # ==================================================
    # 3️⃣ EXISTING APPLICATION CHECK
    # ==================================================
    application = Application.query.filter_by(
        job_id=job.id, worker_id=session["user_id"]
    ).first()

    # 🔁 APPLY AGAIN (revoked / rejected / declined)
    if application and application.status in ["revoked", "rejected", "worker_declined"]:
        application.status = "pending"
        db.session.commit()

        flash("Applied again successfully.", "success")
        return redirect("/dashboard")

    # ❌ Block duplicate active applications
    if application:
        flash("You have already applied for this job.", "warning")
        return redirect("/dashboard")

    # ==================================================
    # 4️⃣ CREATE NEW APPLICATION
    # ==================================================
    new_app = Application(job_id=job.id, worker_id=session["user_id"], status="pending")

    db.session.add(new_app)
    db.session.commit()

    flash("Interest sent. Waiting for manager response.", "success")
    return redirect("/dashboard")


# ======================================================
# REVOKE INTEREST (WORKER) - NEW ROUTE
# ======================================================
@app.route("/job/<int:job_id>/revoke", methods=["POST"])
def worker_revoke_job(job_id):
    if session.get("role") != "worker":
        return redirect("/dashboard")

    application = Application.query.filter_by(
        job_id=job_id, worker_id=session["user_id"], status="pending"
    ).first()

    if not application:
        flash("Nothing to revoke.", "error")
        return redirect("/dashboard")

    application.status = "revoked"
    db.session.commit()

    flash("Application revoked successfully.", "success")
    return redirect("/dashboard")


# ======================================================
# ACCEPT / REJECT APPLICATION (MANAGER) - OLD ROUTES
# ======================================================
@app.route("/application/<int:app_id>/accept")
def manager_accept_worker(app_id):
    if session.get("role") != "manager":
        return redirect("/dashboard")

    application = Application.query.get_or_404(app_id)
    job = Job.query.get_or_404(application.job_id)

    # 🔐 SECURITY: ensure manager owns the job
    if job.manager_id != session["user_id"]:
        flash("Unauthorized action.", "error")
        return redirect("/dashboard")

    # 🚨 CAPACITY CHECK (HARD BLOCK)
    confirmed_count = Application.query.filter_by(
        job_id=job.id, status="worker_confirmed"
    ).count()

    if confirmed_count >= job.required_workers:
        flash(
            f"Job capacity already filled ({confirmed_count}/{job.required_workers}).",
            "error",
        )
        return redirect(f"/job/{job.id}/applications")

    # ✔ Safe to accept
    application.status = "accepted"
    db.session.commit()

    flash("Worker accepted successfully.", "success")
    return redirect(f"/job/{job.id}/applications")


# ======================================================
# REJECT APPLICATION (MANAGER) - OLD ROUTE
# ======================================================


@app.route("/application/<int:app_id>/reject")
def reject_application(app_id):
    if session.get("role") != "manager":
        return redirect("/dashboard")

    app = Application.query.get_or_404(app_id)
    app.status = "rejected"
    db.session.commit()

    return redirect(f"/job/{app.job_id}/applications")


# ======================================================
# OPEN COMPLETE JOB PAGE (GET)
# ======================================================
@app.route("/manager/complete-job/<int:job_id>", methods=["GET"])
def open_complete_job(job_id):
    if session.get("role") != "manager":
        return redirect("/dashboard")

    job = Job.query.get_or_404(job_id)

    # ensure manager owns job
    if job.manager_id != session["user_id"]:
        return redirect("/dashboard")

    return render_template("complete_job.html", job=job)


# ======================================================
# COMPLETE JOB LOGIC (POST)
# ======================================================
@app.route("/manager/complete-job/<int:job_id>", methods=["POST"])
def complete_job(job_id):
    if session.get("role") != "manager":
        return redirect("/dashboard")

    job = Job.query.get_or_404(job_id)

    if job.manager_id != session["user_id"]:
        return redirect("/dashboard")

    rating = request.form.get("rating")
    feedback = request.form.get("feedback")

    report_reason = request.form.get("report_reason")
    report_description = request.form.get("report_description")

    # ✅ ONLY confirmed workers can be completed
    applications = Application.query.filter_by(
        job_id=job.id, status="worker_confirmed"
    ).all()

    if not applications:
        flash("No confirmed workers to complete.", "warning")
        return redirect("/dashboard")

    for app in applications:
        # ⭐ save rating & feedback
        app.rating = rating
        app.feedback = feedback
        app.status = "completed"

        # ⭐ update worker profile
        profile = WorkerProfile.query.filter_by(user_id=app.worker_id).first()

        if profile:
            profile.completed_jobs += 1
            profile.avg_rating = (
                (profile.avg_rating * (profile.completed_jobs - 1)) + int(rating)
            ) / profile.completed_jobs

        # 🚨 optional report
        if report_reason:
            report = Report(
                job_id=job.id,
                reporter_role="manager",
                reported_user_id=app.worker_id,
                reason=report_reason,
                description=report_description,
            )
            db.session.add(report)

    db.session.commit()

    flash("Job completed successfully.", "success")
    return redirect("/dashboard")


# ======================================================
# SUBMIT COMPLETE JOB (POST)
# ======================================================
@app.route("/manager/complete-job/<int:job_id>", methods=["POST"])
def submit_complete_job(job_id):
    if session.get("role") != "manager":
        return redirect("/")

    job = Job.query.get_or_404(job_id)

    if job.manager_id != session.get("user_id"):
        return redirect("/dashboard")

    rating = int(request.form.get("rating"))
    feedback = request.form.get("feedback")

    report_reason = request.form.get("report_reason")
    report_description = request.form.get("report_description")

    applications = Application.query.filter_by(
        job_id=job.id, status="worker_confirmed"
    ).all()

    for app in applications:
        app.status = "completed"
        app.rating = rating
        app.feedback = feedback

        profile = WorkerProfile.query.filter_by(user_id=app.worker_id).first()
        if profile:
            profile.completed_jobs += 1
            profile.avg_rating = (
                (profile.avg_rating * (profile.completed_jobs - 1)) + rating
            ) / profile.completed_jobs

        if report_reason:
            report = Report(
                job_id=job.id,
                reporter_role="manager",
                reported_user_id=app.worker_id,
                reason=report_reason,
                description=report_description,
            )
            db.session.add(report)

    db.session.commit()
    return redirect("/dashboard")


# ======================================================
# AUTO-COMPLETE JOBS FOR WORKER
# ======================================================
from datetime import date, timedelta


def auto_complete_jobs_for_worker(worker_id):
    today = date.today()

    applications = Application.query.filter_by(worker_id=worker_id).all()

    for app in applications:
        job = Job.query.get(app.job_id)
        if not job:
            continue

        end_date = job.work_date + timedelta(days=job.duration_days - 1)

        # 🔁 Auto-complete only AFTER duration
        if today > end_date and app.status != "completed":
            app.status = "completed"
            app.rating = None  # NA
            app.feedback = None  # NA

    db.session.commit()


# ======================================================
# VIEW COMPLETED JOBS (WORKER)
# ======================================================


@app.route("/completed-jobs")
def completed_jobs():
    if session.get("role") != "worker":
        return redirect("/")

    completed_jobs = (
        Application.query.filter_by(worker_id=session["user_id"], status="completed")
        .join(Job)
        .order_by(Job.work_date.desc())
        .all()
    )

    # add AM/PM + shift info safely
    for app in completed_jobs:
        app.job.start_time_ampm = to_ampm(app.job.start_time)
        app.job.end_time_ampm = to_ampm(app.job.end_time)
        app.job.shift_type = detect_shift(app.job.start_time)

    return render_template(
        "completed_jobs.html",
        completed_jobs=completed_jobs,
        helpline_number=HELPLINE_NUMBER,
    )


# ======================================================
# VIEW APPLICANTS (MANAGER)
# ======================================================


@app.route("/job/<int:job_id>/applications")
def view_applications(job_id):
    # 🔐 Manager only
    if session.get("role") != "manager":
        return redirect("/dashboard")

    job = Job.query.get_or_404(job_id)

    # Applications for THIS job
    applications = Application.query.filter_by(job_id=job_id).all()

    enriched_apps = []

    for app in applications:
        worker = User.query.get(app.worker_id)

        # ✅ Worker completed job history
        completed_jobs = (
            Application.query.filter_by(worker_id=worker.id, status="completed")
            .join(Job)
            .all()
        )

        enriched_apps.append({"app": app, "worker": worker, "history": completed_jobs})

    return render_template("applications.html", job=job, enriched_apps=enriched_apps)


# ======================================================
# ACCEPT / REJECT (MANAGER)
# ======================================================


@app.route("/application/<int:app_id>/<action>")
def manage_application(app_id, action):
    if session.get("role") != "manager":
        return redirect("/dashboard")

    # 1️⃣ Get application
    application = Application.query.get_or_404(app_id)

    # 2️⃣ Get related job
    job = Job.query.get_or_404(application.job_id)

    # 3️⃣ Count how many workers are already CONFIRMED (SELECTED)
    confirmed_count = Application.query.filter_by(
        job_id=job.id, status="worker_confirmed"
    ).count()

    # 4️⃣ MANAGER ACCEPT LOGIC
    if action == "accept":
        # ❌ Do NOT accept if capacity full
        if confirmed_count >= job.required_workers:
            # Capacity full → do nothing
            return redirect(f"/job/{job.id}/applications")

        # ✔ Manager accepted worker
        application.status = "accepted"
        db.session.commit()

    # 5️⃣ MANAGER REJECT LOGIC
    elif action == "reject":
        # ✔ Mark rejected (do NOT delete)
        application.status = "rejected"
        db.session.commit()

    # 6️⃣ Redirect back to applicant list
    return redirect(f"/job/{job.id}/applications")


# ======================================================
# WORKER CONFIRM / DECLINE
# ======================================================


@app.route("/worker-response/<int:app_id>/<action>", methods=["POST"])
def worker_response(app_id, action):
    if session.get("role") != "worker":
        return redirect("/dashboard")

    application = Application.query.get_or_404(app_id)
    job = Job.query.get_or_404(application.job_id)

    # 🔐 Security check
    if application.worker_id != session["user_id"]:
        flash("Unauthorized action.", "error")
        return redirect("/dashboard")

    # ==================================================
    # ACCEPT JOB
    # ==================================================
    if action == "accept":

        # ❌ Block multiple jobs on same date
        existing_confirmed = (
            db.session.query(Application)
            .join(Job, Job.id == Application.job_id)
            .filter(
                Application.worker_id == session["user_id"],
                Application.status == "worker_confirmed",
                Job.work_date == job.work_date,
            )
            .first()
        )

        if existing_confirmed:
            flash("You are already hired for another job on this date.", "error")
            return redirect("/dashboard")

        application.status = "worker_confirmed"
        db.session.commit()

        flash("Job accepted successfully.", "success")

    # ==================================================
    # REJECT JOB  ✅ FIXED
    # ==================================================
    elif action == "reject":

        if application.status not in ["accepted", "worker_confirmed"]:
            flash("Invalid reject action.", "error")
            return redirect("/dashboard")

        application.status = "rejected"
        db.session.commit()

        flash("You rejected this job.", "success")

    else:
        flash("Invalid action.", "error")

    return redirect("/dashboard")


# ======================================================
# WORKER VIEW SELECTED JOB
# ======================================================
@app.route("/worker-selected/<int:app_id>")
def worker_selected(app_id):
    if session.get("role") != "worker":
        return redirect("/dashboard")

    application = Application.query.get_or_404(app_id)
    job = Job.query.get_or_404(application.job_id)
    manager = User.query.get_or_404(job.manager_id)

    return render_template(
        "worker_selected.html", application=application, job=job, manager=manager
    )


# ======================================================
# RATE WORKER (MANAGER)
# ======================================================
@app.route("/rate-worker/<int:app_id>", methods=["POST"])
def rate_worker(app_id):
    if session.get("role") != "manager":
        return redirect("/dashboard")

    application = Application.query.get_or_404(app_id)
    job = Job.query.get(application.job_id)

    # 🔒 Security
    if job.manager_id != session["user_id"]:
        return redirect("/dashboard")

    # ⭐ Rating & Feedback
    application.rating = int(request.form.get("rating"))
    application.feedback = request.form.get("feedback")
    application.status = "completed"

    db.session.commit()
    return redirect(f"/job/{job.id}/applications")


# ======================================================
# WORKER SCHEDULE VIEW
# ======================================================


@app.route("/my-schedule")
def my_schedule():
    if session.get("role") != "worker":
        return redirect("/dashboard")

    # Get all CONFIRMED jobs for this worker
    schedule = (
        db.session.query(Application, Job, User)
        .join(Job, Job.id == Application.job_id)
        .join(User, User.id == Job.manager_id)
        .filter(
            Application.worker_id == session["user_id"],
            Application.status == "worker_confirmed",
        )
        .order_by(Job.work_date.asc())
        .all()
    )

    return render_template(
        "worker_schedule.html", schedule=schedule, worker_name=session["name"]
    )


# ======================================================
# JOB DETAILS VIEW
# ======================================================


# @app.route("/job/<int:job_id>/details")
# def job_details(job_id):
#     if "user_id" not in session:
#         return redirect("/")

#     job = Job.query.get_or_404(job_id)
#     manager = User.query.get(job.manager_id)

#     # ==========================
#     # MANAGER → ALWAYS ALLOWED
#     # ==========================
#     if session.get("role") == "manager":
#         confirmed_count = Application.query.filter_by(
#             job_id=job.id, status="worker_confirmed"
#         ).count()

#         return render_template(
#             "job_details.html",
#             job=job,
#             manager=manager,
#             confirmed_count=confirmed_count,
#         )


@app.route("/job/<int:job_id>/details")
def job_details(job_id):
    if "user_id" not in session:
        return redirect("/")

    job = Job.query.get_or_404(job_id)
    manager = User.query.get(job.manager_id)

    # ✅ ADD THESE LINES
    job.start_time_ampm = to_ampm(job.start_time)
    job.end_time_ampm = to_ampm(job.end_time)
    job.shift_type = detect_shift(job.start_time)

    # ==========================
    # MANAGER → ALWAYS ALLOWED
    # ==========================
    if session.get("role") == "manager":
        confirmed_count = Application.query.filter_by(
            job_id=job.id, status="worker_confirmed"
        ).count()

        return render_template(
            "job_details.html",
            job=job,
            manager=manager,
            confirmed_count=confirmed_count,
        )

    # ==========================
    # WORKER → ACCEPTED OR CONFIRMED
    # ==========================
    if session.get("role") == "worker":
        application = (
            Application.query.filter_by(
                job_id=job.id,
                worker_id=session["user_id"],
            )
            .filter(Application.status.in_(["accepted", "worker_confirmed"]))
            .first()
        )

        if not application:
            return redirect("/dashboard")

        confirmed_count = Application.query.filter_by(
            job_id=job.id, status="worker_confirmed"
        ).count()

        return render_template(
            "job_details.html",
            job=job,
            manager=manager,
            confirmed_count=confirmed_count,
            application=application,
            role="worker",
        )

    return redirect("/")

    # ==========================
    # WORKER → ACCEPTED OR CONFIRMED
    # ==========================
    if session.get("role") == "worker":
        application = (
            Application.query.filter_by(
                job_id=job.id,
                worker_id=session["user_id"],
            )
            .filter(Application.status.in_(["accepted", "worker_confirmed"]))
            .first()
        )

        # ❌ Block if NOT accepted by manager
        if not application:
            return redirect("/dashboard")

        confirmed_count = Application.query.filter_by(
            job_id=job.id, status="worker_confirmed"
        ).count()

        return render_template(
            "job_details.html",
            job=job,
            manager=manager,
            confirmed_count=confirmed_count,
            application=application,
            role="worker",
        )

    # ==========================
    # FALLBACK (NON-WORKER)
    # ==========================
    return redirect("/")


# ======================================================
# WORKER ACCEPT JOB
# ======================================================
@app.route("/job/<int:job_id>/accept")
def worker_accept_job(job_id):
    if session.get("role") != "worker":
        return redirect("/dashboard")

    job = Job.query.get_or_404(job_id)

    application = Application.query.filter_by(
        job_id=job.id, worker_id=session["user_id"], status="accepted"
    ).first_or_404()

    # 🔒 CHECK: already hired on same date?
    conflict = (
        Application.query.join(Job, Application.job_id == Job.id)
        .filter(
            Application.worker_id == session["user_id"],
            Application.status.in_(["worker_confirmed", "completed"]),
            Job.work_date == job.work_date,
        )
        .first()
    )

    if conflict:
        flash("❌ You are already hired for another job on this date.", "error")
        return redirect("/dashboard")

    # ✅ Confirm job
    application.status = "worker_confirmed"
    db.session.commit()

    flash("✅ Job accepted successfully.", "success")
    return redirect("/dashboard")


# ======================================================
# WORKER REJECT JOB
# ======================================================


@app.route("/job/<int:job_id>/worker-reject", methods=["POST"])
def worker_reject_job(job_id):
    if session.get("role") != "worker":
        return redirect("/dashboard")

    application = (
        Application.query.filter_by(job_id=job_id, worker_id=session["user_id"])
        .filter(Application.status.in_(["accepted", "worker_confirmed"]))
        .first()
    )

    if not application:
        flash("Invalid action.", "error")
        return redirect("/dashboard")

    application.status = "rejected"
    db.session.commit()

    flash("You rejected this job.", "success")
    return redirect("/dashboard")


# ======================================================
# SET LANGUAGE
# ======================================================
@app.route("/set-language/<lang>")
def set_language(lang):
    if lang in ["en", "hi"]:
        session["lang"] = lang
    return redirect(request.referrer or "/dashboard")


# ======================================================
# LOGOUT
# ======================================================


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ======================================================
# RUN
# ======================================================

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    # host="0.0.0.0" allows access from other devices on the same network
    app.run(debug=True, host="0.0.0.0", port=5000)
