# routes/auth.py - login, logout, student and examiner registration
import sqlite3

from flask import (Blueprint, flash, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_db

auth_bp = Blueprint("auth", __name__)

# Where each role lands after login
DASHBOARDS = {
    "admin": "admin.dashboard",
    "examiner": "examiner.dashboard",
    "student": "student.dashboard",
}


@auth_bp.route("/")
def index():
    """Send logged-in users to their dashboard, others to login."""
    role = session.get("role")
    if role in DASHBOARDS:
        return redirect(url_for(DASHBOARDS[role]))
    return redirect(url_for("auth.login"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = get_db().execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()

        # Same message for unknown email and wrong password (no user enumeration)
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_template("auth/login.html", email=email)

        # Block examiners who are not approved (or were deactivated)
        if user["status"] == "Pending":
            flash("Your account is awaiting admin approval.", "warning")
            return render_template("auth/login.html", email=email)
        if user["status"] == "Deactivated":
            flash("Your account has been deactivated. Contact the admin.", "error")
            return render_template("auth/login.html", email=email)

        session.clear()                      # fresh session on every login
        session["user_id"] = user["id"]
        session["role"] = user["role"]
        session["name"] = user["name"]
        flash(f"Welcome, {user['name']}!", "success")
        return redirect(url_for(DASHBOARDS[user["role"]]))

    return render_template("auth/login.html", email="")


@auth_bp.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


def _validate_common(form, db):
    """Checks shared by both registration forms. Returns a list of error strings."""
    errors = []
    name = form.get("name", "").strip()
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    confirm = form.get("confirm_password", "")

    if not name:
        errors.append("Name is required.")
    if not email or "@" not in email:
        errors.append("A valid email is required.")
    elif db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        errors.append("This email is already registered.")
    if len(password) < 6:
        errors.append("Password must be at least 6 characters.")
    if password != confirm:
        errors.append("Passwords do not match.")
    return errors


@auth_bp.route("/register/student", methods=["GET", "POST"])
def register_student():
    if request.method == "POST":
        db = get_db()
        f = request.form
        errors = _validate_common(f, db)

        roll_no = f.get("roll_no", "").strip().upper()
        program = f.get("program", "").strip()
        year = f.get("year", "").strip()

        if not roll_no:
            errors.append("Roll number is required.")
        elif db.execute("SELECT 1 FROM student_profiles WHERE roll_no = ?", (roll_no,)).fetchone():
            errors.append("This roll number is already registered.")
        if not program:
            errors.append("Program is required.")
        if not year.isdigit() or not (1 <= int(year) <= 6):
            errors.append("Year must be a number between 1 and 6.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/register_student.html", form=f)

        try:
            # Two inserts, one transaction: both succeed or neither does
            cur = db.execute(
                "INSERT INTO users (name, email, password_hash, role, phone, status) "
                "VALUES (?, ?, ?, 'student', ?, 'Active')",
                (f["name"].strip(), f["email"].strip().lower(),
                 generate_password_hash(f["password"]), f.get("phone", "").strip()),
            )
            db.execute(
                "INSERT INTO student_profiles (user_id, roll_no, program, year) "
                "VALUES (?, ?, ?, ?)",
                (cur.lastrowid, roll_no, program, int(year)),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            flash("Registration failed: email or roll number already exists.", "error")
            return render_template("auth/register_student.html", form=f)

        flash("Registration successful. You can log in now.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/register_student.html", form={})


@auth_bp.route("/register/examiner", methods=["GET", "POST"])
def register_examiner():
    if request.method == "POST":
        db = get_db()
        f = request.form
        errors = _validate_common(f, db)

        department = f.get("department", "").strip()
        designation = f.get("designation", "").strip()
        if not department:
            errors.append("Department is required.")
        if not designation:
            errors.append("Designation is required.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/register_examiner.html", form=f)

        try:
            cur = db.execute(
                "INSERT INTO users (name, email, password_hash, role, phone, status) "
                "VALUES (?, ?, ?, 'examiner', ?, 'Pending')",
                (f["name"].strip(), f["email"].strip().lower(),
                 generate_password_hash(f["password"]), f.get("phone", "").strip()),
            )
            db.execute(
                "INSERT INTO examiner_profiles (user_id, department, designation, contact_details) "
                "VALUES (?, ?, ?, ?)",
                (cur.lastrowid, department, designation, f.get("contact_details", "").strip()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            flash("Registration failed: this email already exists.", "error")
            return render_template("auth/register_examiner.html", form=f)

        flash("Registration submitted. You can log in once the admin approves you.", "info")
        return redirect(url_for("auth.login"))

    return render_template("auth/register_examiner.html", form={})
