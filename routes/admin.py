# routes/admin.py - all /admin routes
# Phase 3: dashboard, courses, examinations, rubrics, examiner approval + assignment
# Phase 6-7: publishing, students, slots, bookings, reschedule, change examiner, search
import sqlite3
from datetime import date, timedelta

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, session, url_for)

from database import get_db
from helpers import (examiner_has_clash, refresh_exam_status, reschedule_booking,
                     role_required, today_iso)

admin_bp = Blueprint("admin", __name__)

EXAM_TYPES = ["Viva", "Practical", "Project Demo", "Assessment"]
EXAM_STATUSES = ["Draft", "Slot Creation", "Booking Open", "Closed", "Completed"]


# ----------------------------- small helpers -----------------------------

def _to_int(value, minimum=1):
    """Convert text to int (>= minimum) or return None."""
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    return n if n >= minimum else None


def _to_float(value):
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _to_date(value):
    """Validate YYYY-MM-DD text; return it or None."""
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        return None


def _get_exam_or_404(db, exam_id):
    exam = db.execute(
        "SELECT e.*, c.course_code, c.course_name FROM examinations e "
        "JOIN courses c ON c.id = e.course_id WHERE e.id = ?", (exam_id,)
    ).fetchone()
    if exam is None:
        abort(404)
    return exam


# ------------------------------- dashboard -------------------------------

@admin_bp.route("/dashboard")
@role_required("admin")
def dashboard():
    db = get_db()
    count = lambda sql: db.execute(sql).fetchone()[0]
    stats = {
        "Courses": count("SELECT COUNT(*) FROM courses"),
        "Examinations": count("SELECT COUNT(*) FROM examinations"),
        "Examiners": count("SELECT COUNT(*) FROM users WHERE role='examiner' AND status='Active'"),
        "Students": count("SELECT COUNT(*) FROM users WHERE role='student'"),
        "Slots": count("SELECT COUNT(*) FROM slots"),
        "Bookings": count("SELECT COUNT(*) FROM bookings"),
    }
    pending = db.execute(
        "SELECT u.id, u.name, u.email, u.created_at, p.department, p.designation "
        "FROM users u JOIN examiner_profiles p ON p.user_id = u.id "
        "WHERE u.role='examiner' AND u.status='Pending' ORDER BY u.created_at"
    ).fetchall()
    return render_template("admin/dashboard.html", stats=stats, pending=pending)


# -------------------------------- courses --------------------------------

def _read_course_form(f, db, course_id=None):
    """Validate the course form. Returns (clean_dict, errors)."""
    errors = []
    code = f.get("course_code", "").strip().upper()
    name = f.get("course_name", "").strip()
    credits = _to_int(f.get("credits", ""), 1)
    status = f.get("status", "Active")

    if not code:
        errors.append("Course code is required.")
    else:
        dup = db.execute(
            "SELECT id FROM courses WHERE course_code = ? AND id != ?", (code, course_id or 0)
        ).fetchone()
        if dup:
            errors.append("Another course already uses this code.")
    if not name:
        errors.append("Course name is required.")
    if credits is None or credits > 20:
        errors.append("Credits must be a whole number between 1 and 20.")
    if status not in ("Active", "Inactive"):
        errors.append("Invalid status.")
    return {"course_code": code, "course_name": name, "credits": credits,
            "description": f.get("description", "").strip(), "status": status}, errors


@admin_bp.route("/courses")
@role_required("admin")
def courses():
    rows = get_db().execute(
        "SELECT c.*, (SELECT COUNT(*) FROM examinations e WHERE e.course_id = c.id) AS exam_count "
        "FROM courses c ORDER BY c.course_code"
    ).fetchall()
    return render_template("admin/courses.html", courses=rows)


@admin_bp.route("/courses/add", methods=["GET", "POST"])
@role_required("admin")
def add_course():
    db = get_db()
    if request.method == "POST":
        data, errors = _read_course_form(request.form, db)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/course_form.html", form=request.form, editing=False)
        db.execute(
            "INSERT INTO courses (course_code, course_name, description, credits, status) "
            "VALUES (:course_code, :course_name, :description, :credits, :status)", data)
        db.commit()
        flash("Course created.", "success")
        return redirect(url_for("admin.courses"))
    return render_template("admin/course_form.html", form={"status": "Active"}, editing=False)


@admin_bp.route("/courses/<int:course_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_course(course_id):
    db = get_db()
    course = db.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if course is None:
        abort(404)
    if request.method == "POST":
        data, errors = _read_course_form(request.form, db, course_id)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/course_form.html", form=request.form, editing=True, course=course)
        data["id"] = course_id
        db.execute(
            "UPDATE courses SET course_code=:course_code, course_name=:course_name, "
            "description=:description, credits=:credits, status=:status WHERE id=:id", data)
        db.commit()
        flash("Course updated.", "success")
        return redirect(url_for("admin.courses"))
    return render_template("admin/course_form.html", form=dict(course), editing=True, course=course)


@admin_bp.route("/courses/<int:course_id>/delete", methods=["GET", "POST"])
@role_required("admin")
def delete_course(course_id):
    db = get_db()
    course = db.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if course is None:
        abort(404)
    # Business rule: a course that has examinations cannot be removed
    exams = db.execute("SELECT COUNT(*) FROM examinations WHERE course_id = ?", (course_id,)).fetchone()[0]
    if exams:
        flash(f"Cannot delete {course['course_code']}: it has {exams} examination(s). "
              "Mark it Inactive instead.", "error")
        return redirect(url_for("admin.courses"))
    if request.method == "POST":
        db.execute("DELETE FROM courses WHERE id = ?", (course_id,))
        db.commit()
        flash("Course deleted.", "success")
        return redirect(url_for("admin.courses"))
    return render_template("admin/confirm_delete.html",
                           what=f"course {course['course_code']} - {course['course_name']}",
                           cancel_url=url_for("admin.courses"))


# ----------------------------- examinations ------------------------------

def _read_exam_form(f, db):
    """Validate the exam form. Returns (clean_dict, errors).
    clean['status'] is 'auto' or one of EXAM_STATUSES."""
    errors = []
    course_id = _to_int(f.get("course_id", ""), 1)
    name = f.get("exam_name", "").strip()
    exam_type = f.get("exam_type", "")
    duration = _to_int(f.get("duration_minutes", ""), 1)
    max_marks = _to_int(f.get("max_marks", ""), 1)
    scs = _to_date(f.get("slot_creation_start", ""))
    sce = _to_date(f.get("slot_creation_end", ""))
    bs = _to_date(f.get("booking_start", ""))
    be = _to_date(f.get("booking_end", ""))
    status = f.get("status", "auto")

    if course_id is None or db.execute("SELECT 1 FROM courses WHERE id = ?", (course_id,)).fetchone() is None:
        errors.append("Choose a valid course.")
    if not name:
        errors.append("Examination name is required.")
    if exam_type not in EXAM_TYPES:
        errors.append("Choose a valid examination type.")
    if duration is None:
        errors.append("Duration must be a positive whole number of minutes.")
    if max_marks is None:
        errors.append("Maximum marks must be a positive whole number.")
    if None in (scs, sce, bs, be):
        errors.append("All four timeline dates are required and must be valid.")
    else:
        if sce < scs:
            errors.append("Slot creation end cannot be before slot creation start.")
        if be < bs:
            errors.append("Booking end cannot be before booking start.")
        if bs < scs:
            errors.append("Booking cannot start before slot creation starts.")
    if status != "auto" and status not in EXAM_STATUSES:
        errors.append("Invalid status.")

    return {"course_id": course_id, "exam_name": name, "exam_type": exam_type,
            "duration_minutes": duration, "max_marks": max_marks,
            "slot_creation_start": scs, "slot_creation_end": sce,
            "booking_start": bs, "booking_end": be,
            "venue": f.get("venue", "").strip(),
            "instructions": f.get("instructions", "").strip(),
            "status": status}, errors


def _course_choices(db, include_id=None):
    """Active courses (plus the exam's current course when editing)."""
    return db.execute(
        "SELECT id, course_code, course_name FROM courses WHERE status = 'Active' OR id = ? "
        "ORDER BY course_code", (include_id or 0,)).fetchall()


@admin_bp.route("/exams")
@role_required("admin")
def exams():
    rows = get_db().execute(
        "SELECT e.*, c.course_code, c.course_name, "
        " (SELECT COUNT(*) FROM exam_examiners x WHERE x.exam_id = e.id) AS examiner_count, "
        " (SELECT COUNT(*) FROM rubrics r WHERE r.exam_id = e.id) AS rubric_count "
        "FROM examinations e JOIN courses c ON c.id = e.course_id ORDER BY e.id DESC"
    ).fetchall()
    return render_template("admin/exams.html", exams=rows)


@admin_bp.route("/exams/add", methods=["GET", "POST"])
@role_required("admin")
def add_exam():
    db = get_db()
    if request.method == "POST":
        data, errors = _read_exam_form(request.form, db)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/exam_form.html", form=request.form, editing=False,
                                   courses=_course_choices(db), types=EXAM_TYPES, statuses=EXAM_STATUSES)
        override = 0 if data["status"] == "auto" else 1
        data["status_override"] = override
        data["status"] = "Draft" if override == 0 else data["status"]
        cur = db.execute(
            "INSERT INTO examinations (course_id, exam_name, exam_type, duration_minutes, max_marks, "
            " slot_creation_start, slot_creation_end, booking_start, booking_end, venue, instructions, "
            " status, status_override) "
            "VALUES (:course_id, :exam_name, :exam_type, :duration_minutes, :max_marks, "
            " :slot_creation_start, :slot_creation_end, :booking_start, :booking_end, :venue, "
            " :instructions, :status, :status_override)", data)
        refresh_exam_status(db, db.execute("SELECT * FROM examinations WHERE id = ?", (cur.lastrowid,)).fetchone())
        db.commit()
        flash("Examination created. Next: add rubrics and assign examiners.", "success")
        return redirect(url_for("admin.exams"))
    return render_template("admin/exam_form.html", form={"status": "auto"}, editing=False,
                           courses=_course_choices(db), types=EXAM_TYPES, statuses=EXAM_STATUSES)


@admin_bp.route("/exams/<int:exam_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_exam(exam_id):
    db = get_db()
    exam = _get_exam_or_404(db, exam_id)
    if request.method == "POST":
        data, errors = _read_exam_form(request.form, db)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/exam_form.html", form=request.form, editing=True, exam=exam,
                                   courses=_course_choices(db, exam["course_id"]),
                                   types=EXAM_TYPES, statuses=EXAM_STATUSES)
        override = 0 if data["status"] == "auto" else 1
        data["status_override"] = override
        data["status"] = exam["status"] if override == 0 else data["status"]
        data["id"] = exam_id
        db.execute(
            "UPDATE examinations SET course_id=:course_id, exam_name=:exam_name, exam_type=:exam_type, "
            " duration_minutes=:duration_minutes, max_marks=:max_marks, "
            " slot_creation_start=:slot_creation_start, slot_creation_end=:slot_creation_end, "
            " booking_start=:booking_start, booking_end=:booking_end, venue=:venue, "
            " instructions=:instructions, status=:status, status_override=:status_override "
            "WHERE id=:id", data)
        refresh_exam_status(db, db.execute("SELECT * FROM examinations WHERE id = ?", (exam_id,)).fetchone())
        db.commit()
        flash("Examination updated.", "success")
        return redirect(url_for("admin.exams"))

    form = dict(exam)
    form["status"] = exam["status"] if exam["status_override"] else "auto"
    return render_template("admin/exam_form.html", form=form, editing=True, exam=exam,
                           courses=_course_choices(db, exam["course_id"]),
                           types=EXAM_TYPES, statuses=EXAM_STATUSES)


@admin_bp.route("/exams/<int:exam_id>/delete", methods=["GET", "POST"])
@role_required("admin")
def delete_exam(exam_id):
    db = get_db()
    exam = _get_exam_or_404(db, exam_id)
    # Slots (and therefore bookings) must not be orphaned
    slots = db.execute("SELECT COUNT(*) FROM slots WHERE exam_id = ?", (exam_id,)).fetchone()[0]
    if slots:
        flash(f"Cannot delete this examination: it has {slots} slot(s). "
              "Close it instead to keep the history.", "error")
        return redirect(url_for("admin.exams"))
    if request.method == "POST":
        # Rubrics and examiner assignments belong to the exam, so they go with it
        db.execute("DELETE FROM rubrics WHERE exam_id = ?", (exam_id,))
        db.execute("DELETE FROM exam_examiners WHERE exam_id = ?", (exam_id,))
        db.execute("DELETE FROM examinations WHERE id = ?", (exam_id,))
        db.commit()
        flash("Examination deleted.", "success")
        return redirect(url_for("admin.exams"))
    return render_template("admin/confirm_delete.html",
                           what=f"examination {exam['exam_name']} ({exam['course_code']}) "
                                "together with its rubrics and examiner assignments",
                           cancel_url=url_for("admin.exams"))


@admin_bp.route("/exams/<int:exam_id>/status", methods=["POST"])
@role_required("admin")
def exam_status(exam_id):
    """Open/close slot creation or booking by moving the exam's dates.
    Business rules read the dates, so changing the dates keeps rules and status consistent.
    These buttons also hand control back to the automatic status."""
    db = get_db()
    exam = _get_exam_or_404(db, exam_id)
    action = request.form.get("action", "")
    t = today_iso()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    scs, sce = exam["slot_creation_start"], exam["slot_creation_end"]
    bs, be = exam["booking_start"], exam["booking_end"]

    if action == "open_slots":
        scs, sce = min(scs, t), max(sce, t)
        msg = "Slot creation is now open."
    elif action == "close_slots":
        sce = yesterday
        scs = min(scs, sce)
        msg = "Slot creation is now closed."
    elif action == "open_booking":
        bs, be = min(bs, t), max(be, t)
        scs = min(scs, bs)
        msg = "Student booking is now open."
    elif action == "close_booking":
        be = yesterday
        bs = min(bs, be)
        msg = "Student booking is now closed."
    elif action == "auto":
        msg = "Status will follow the dates automatically."
    else:
        abort(400)

    db.execute(
        "UPDATE examinations SET slot_creation_start=?, slot_creation_end=?, booking_start=?, "
        "booking_end=?, status_override=0 WHERE id=?", (scs, sce, bs, be, exam_id))
    refresh_exam_status(db, db.execute("SELECT * FROM examinations WHERE id = ?", (exam_id,)).fetchone())
    db.commit()
    flash(msg, "success")
    return redirect(url_for("admin.edit_exam", exam_id=exam_id))


# -------------------------------- rubrics --------------------------------

def _read_rubric_form(f):
    errors = []
    name = f.get("criterion_name", "").strip()
    max_marks = _to_float(f.get("max_marks", ""))
    weight_raw = f.get("weightage", "").strip()
    weight = _to_float(weight_raw) if weight_raw else None

    if not name:
        errors.append("Criterion name is required.")
    if max_marks is None or max_marks <= 0:
        errors.append("Maximum marks must be a number greater than 0.")
    if weight_raw and (weight is None or not 0 <= weight <= 100):
        errors.append("Weightage must be a number between 0 and 100 (or left blank).")
    return {"criterion_name": name, "max_marks": max_marks, "weightage": weight,
            "description": f.get("description", "").strip()}, errors


@admin_bp.route("/exams/<int:exam_id>/rubrics")
@role_required("admin")
def rubrics(exam_id):
    db = get_db()
    exam = _get_exam_or_404(db, exam_id)
    rows = db.execute("SELECT * FROM rubrics WHERE exam_id = ? ORDER BY id", (exam_id,)).fetchall()
    total = sum(r["max_marks"] for r in rows)
    # Warning (not an error) when criteria marks do not add up to the exam maximum
    mismatch = bool(rows) and abs(total - exam["max_marks"]) > 1e-9
    return render_template("admin/rubrics.html", exam=exam, rubrics=rows, total=total, mismatch=mismatch)


@admin_bp.route("/exams/<int:exam_id>/rubrics/add", methods=["GET", "POST"])
@role_required("admin")
def add_rubric(exam_id):
    db = get_db()
    exam = _get_exam_or_404(db, exam_id)
    if request.method == "POST":
        data, errors = _read_rubric_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/rubric_form.html", exam=exam, form=request.form, editing=False)
        data["exam_id"] = exam_id
        db.execute("INSERT INTO rubrics (exam_id, criterion_name, max_marks, weightage, description) "
                   "VALUES (:exam_id, :criterion_name, :max_marks, :weightage, :description)", data)
        db.commit()
        flash("Rubric criterion added.", "success")
        return redirect(url_for("admin.rubrics", exam_id=exam_id))
    return render_template("admin/rubric_form.html", exam=exam, form={}, editing=False)


def _get_rubric_or_404(db, exam_id, rubric_id):
    r = db.execute("SELECT * FROM rubrics WHERE id = ? AND exam_id = ?", (rubric_id, exam_id)).fetchone()
    if r is None:
        abort(404)
    return r


@admin_bp.route("/exams/<int:exam_id>/rubrics/<int:rubric_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_rubric(exam_id, rubric_id):
    db = get_db()
    exam = _get_exam_or_404(db, exam_id)
    rubric = _get_rubric_or_404(db, exam_id, rubric_id)
    if request.method == "POST":
        data, errors = _read_rubric_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/rubric_form.html", exam=exam, form=request.form, editing=True)
        data["id"] = rubric_id
        db.execute("UPDATE rubrics SET criterion_name=:criterion_name, max_marks=:max_marks, "
                   "weightage=:weightage, description=:description WHERE id=:id", data)
        db.commit()
        flash("Rubric criterion updated.", "success")
        return redirect(url_for("admin.rubrics", exam_id=exam_id))
    return render_template("admin/rubric_form.html", exam=exam, form=dict(rubric), editing=True)


@admin_bp.route("/exams/<int:exam_id>/rubrics/<int:rubric_id>/delete", methods=["GET", "POST"])
@role_required("admin")
def delete_rubric(exam_id, rubric_id):
    db = get_db()
    _get_exam_or_404(db, exam_id)
    rubric = _get_rubric_or_404(db, exam_id, rubric_id)
    used = db.execute("SELECT COUNT(*) FROM evaluation_scores WHERE rubric_id = ?", (rubric_id,)).fetchone()[0]
    if used:
        flash("Cannot delete this criterion: marks have already been awarded against it.", "error")
        return redirect(url_for("admin.rubrics", exam_id=exam_id))
    if request.method == "POST":
        db.execute("DELETE FROM rubrics WHERE id = ?", (rubric_id,))
        db.commit()
        flash("Rubric criterion deleted.", "success")
        return redirect(url_for("admin.rubrics", exam_id=exam_id))
    return render_template("admin/confirm_delete.html",
                           what=f"rubric criterion \"{rubric['criterion_name']}\"",
                           cancel_url=url_for("admin.rubrics", exam_id=exam_id))


# ------------------------------- examiners -------------------------------

@admin_bp.route("/examiners")
@role_required("admin")
def examiners():
    status = request.args.get("status", "")
    sql = ("SELECT u.id AS user_id, u.name, u.email, u.phone, u.status, u.created_at, "
           " p.id AS profile_id, p.department, p.designation, "
           " (SELECT COUNT(*) FROM exam_examiners x WHERE x.examiner_id = p.id) AS exam_count "
           "FROM users u JOIN examiner_profiles p ON p.user_id = u.id WHERE u.role = 'examiner'")
    params = []
    if status in ("Active", "Pending", "Deactivated"):
        sql += " AND u.status = ?"
        params.append(status)
    rows = get_db().execute(sql + " ORDER BY u.status, u.name", params).fetchall()
    return render_template("admin/examiners.html", examiners=rows, status=status)


# action -> (allowed current statuses, new status, flash message)
EXAMINER_ACTIONS = {
    "approve":    (("Pending", "Deactivated"), "Active", "Examiner approved."),
    "reject":     (("Pending",), "Deactivated", "Examiner request rejected."),
    "deactivate": (("Active",), "Deactivated", "Examiner deactivated."),
}


@admin_bp.route("/examiners/<int:user_id>/<action>", methods=["POST"])
@role_required("admin")
def examiner_action(user_id, action):
    if action not in EXAMINER_ACTIONS:
        abort(404)
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ? AND role = 'examiner'", (user_id,)).fetchone()
    if user is None:
        abort(404)
    allowed, new_status, msg = EXAMINER_ACTIONS[action]
    if user["status"] not in allowed:
        flash(f"Cannot {action} an examiner whose status is {user['status']}.", "error")
    else:
        db.execute("UPDATE users SET status = ? WHERE id = ?", (new_status, user_id))
        db.commit()
        flash(msg, "success")
    # Whitelisted return page (never redirect to a user-supplied URL)
    back = "admin.dashboard" if request.form.get("back") == "dashboard" else "admin.examiners"
    return redirect(url_for(back))


@admin_bp.route("/exams/<int:exam_id>/assign", methods=["GET", "POST"])
@role_required("admin")
def assign(exam_id):
    db = get_db()
    exam = _get_exam_or_404(db, exam_id)

    if request.method == "POST":
        action = request.form.get("action")
        profile_id = _to_int(request.form.get("examiner_id", ""), 1)
        profile = db.execute(
            "SELECT p.id, u.status FROM examiner_profiles p JOIN users u ON u.id = p.user_id "
            "WHERE p.id = ?", (profile_id or 0,)).fetchone()
        if profile is None:
            flash("Examiner not found.", "error")
        elif action == "add":
            if profile["status"] != "Active":
                flash("Only approved (Active) examiners can be assigned.", "error")
            else:
                try:
                    db.execute("INSERT INTO exam_examiners (exam_id, examiner_id) VALUES (?, ?)",
                               (exam_id, profile_id))
                    db.commit()
                    flash("Examiner assigned.", "success")
                except sqlite3.IntegrityError:
                    flash("This examiner is already assigned to the examination.", "warning")
        elif action == "remove":
            has_slots = db.execute("SELECT COUNT(*) FROM slots WHERE exam_id = ? AND examiner_id = ?",
                                   (exam_id, profile_id)).fetchone()[0]
            if has_slots:
                flash("Cannot remove: this examiner already has slots for this examination.", "error")
            else:
                db.execute("DELETE FROM exam_examiners WHERE exam_id = ? AND examiner_id = ?",
                           (exam_id, profile_id))
                db.commit()
                flash("Examiner removed from the examination.", "success")
        else:
            abort(400)
        return redirect(url_for("admin.assign", exam_id=exam_id))

    assigned = db.execute(
        "SELECT p.id AS profile_id, u.name, u.email, u.status, p.department, p.designation, "
        " (SELECT COUNT(*) FROM slots s WHERE s.exam_id = x.exam_id AND s.examiner_id = p.id) AS slot_count "
        "FROM exam_examiners x JOIN examiner_profiles p ON p.id = x.examiner_id "
        "JOIN users u ON u.id = p.user_id WHERE x.exam_id = ? ORDER BY u.name", (exam_id,)).fetchall()
    available = db.execute(
        "SELECT p.id AS profile_id, u.name, p.department FROM examiner_profiles p "
        "JOIN users u ON u.id = p.user_id WHERE u.status = 'Active' "
        "AND p.id NOT IN (SELECT examiner_id FROM exam_examiners WHERE exam_id = ?) ORDER BY u.name",
        (exam_id,)).fetchall()
    return render_template("admin/assign.html", exam=exam, assigned=assigned, available=available)


# ------------------------- evaluations & publishing -------------------------

@admin_bp.route("/evaluations")
@role_required("admin")
def evaluations():
    db = get_db()
    exam = request.args.get("exam", "").strip()
    sql = ("SELECT ev.*, e.id AS exam_id, e.exam_name, e.max_marks, c.course_code, su.name AS student_name, "
           " sp.roll_no, xu.name AS examiner_name "
           "FROM evaluations ev JOIN bookings b ON b.id = ev.booking_id JOIN examinations e ON e.id = b.exam_id "
           "JOIN courses c ON c.id = e.course_id JOIN users su ON su.id = b.student_id "
           "LEFT JOIN student_profiles sp ON sp.user_id = su.id "
           "JOIN examiner_profiles p ON p.id = ev.examiner_id JOIN users xu ON xu.id = p.user_id WHERE 1=1")
    params = []
    if exam.isdigit():
        sql += " AND e.id = ?"
        params.append(int(exam))
    rows = db.execute(sql + " ORDER BY e.id, su.name", params).fetchall()
    exams_ = db.execute(
        "SELECT e.id, e.exam_name, c.course_code, "
        " (SELECT COUNT(*) FROM evaluations ev JOIN bookings b ON b.id = ev.booking_id WHERE b.exam_id = e.id) AS total, "
        " (SELECT COUNT(*) FROM evaluations ev JOIN bookings b ON b.id = ev.booking_id "
        "   WHERE b.exam_id = e.id AND ev.is_published = 1) AS published "
        "FROM examinations e JOIN courses c ON c.id = e.course_id ORDER BY e.id DESC").fetchall()
    return render_template("admin/evaluations.html", rows=rows, exams=exams_, exam=exam)


@admin_bp.route("/evaluations/<int:eval_id>/<action>", methods=["POST"])
@role_required("admin")
def evaluation_publish(eval_id, action):
    """Publish / unpublish ONE submitted evaluation."""
    if action not in ("publish", "unpublish"):
        abort(404)
    db = get_db()
    ev = db.execute("SELECT * FROM evaluations WHERE id = ?", (eval_id,)).fetchone()
    if ev is None:
        abort(404)
    if ev["status"] != "Submitted":
        flash("Only submitted evaluations can be published.", "error")
    else:
        db.execute("UPDATE evaluations SET is_published = ? WHERE id = ?", (1 if action == "publish" else 0, eval_id))
        db.commit()
        flash("Result published." if action == "publish" else "Result unpublished.", "success")
    exam = request.form.get("exam", "")
    return redirect(url_for("admin.evaluations", exam=exam) if exam.isdigit() else url_for("admin.evaluations"))


@admin_bp.route("/exams/<int:exam_id>/publish", methods=["POST"])
@role_required("admin")
def exam_publish(exam_id):
    """Publish / unpublish all submitted evaluations of one examination in one click."""
    db = get_db()
    _get_exam_or_404(db, exam_id)
    action = request.form.get("action")
    if action not in ("publish_all", "unpublish_all"):
        abort(400)
    cur = db.execute(
        "UPDATE evaluations SET is_published = ? WHERE status = 'Submitted' AND is_published = ? "
        "AND booking_id IN (SELECT id FROM bookings WHERE exam_id = ?)",
        (1 if action == "publish_all" else 0, 0 if action == "publish_all" else 1, exam_id))
    db.commit()
    flash(f"{cur.rowcount} result(s) {'published' if action == 'publish_all' else 'unpublished'}.", "success")
    return redirect(url_for("admin.evaluations", exam=exam_id))


# --------------------------------- students ---------------------------------

@admin_bp.route("/students")
@role_required("admin")
def students():
    q = request.args.get("q", "").strip()
    sql = ("SELECT u.id, u.name, u.email, u.phone, u.status, u.created_at, p.roll_no, p.program, p.year, "
           " (SELECT COUNT(*) FROM bookings b WHERE b.student_id = u.id) AS booking_count "
           "FROM users u JOIN student_profiles p ON p.user_id = u.id WHERE u.role = 'student'")
    params = []
    if q:
        sql += " AND (u.name LIKE ? OR u.email LIKE ? OR p.roll_no LIKE ?)"
        params += [f"%{q}%"] * 3
    rows = get_db().execute(sql + " ORDER BY p.roll_no", params).fetchall()
    return render_template("admin/students.html", students=rows, q=q)


# ---------------------------- slots and bookings ----------------------------

def _filters():
    """Read the four list filters (exam, examiner, date, status) from the query string."""
    a = request.args
    return {"exam": a.get("exam", "").strip(), "examiner": a.get("examiner", "").strip(),
            "date": _to_date(a.get("date", "")) or "", "status": a.get("status", "").strip()}


def _filter_choices(db):
    exams_ = db.execute("SELECT e.id, e.exam_name, c.course_code FROM examinations e "
                        "JOIN courses c ON c.id = e.course_id ORDER BY e.id DESC").fetchall()
    examiners_ = db.execute("SELECT p.id, u.name FROM examiner_profiles p JOIN users u ON u.id = p.user_id "
                            "ORDER BY u.name").fetchall()
    return exams_, examiners_


@admin_bp.route("/slots")
@role_required("admin")
def slots():
    db = get_db()
    f = _filters()
    sql = ("SELECT s.*, e.exam_name, c.course_code, u.name AS examiner_name "
           "FROM slots s JOIN examinations e ON e.id = s.exam_id JOIN courses c ON c.id = e.course_id "
           "JOIN examiner_profiles p ON p.id = s.examiner_id JOIN users u ON u.id = p.user_id WHERE 1=1")
    params = []
    if f["exam"].isdigit():
        sql += " AND s.exam_id = ?"; params.append(int(f["exam"]))
    if f["examiner"].isdigit():
        sql += " AND s.examiner_id = ?"; params.append(int(f["examiner"]))
    if f["date"]:
        sql += " AND s.slot_date = ?"; params.append(f["date"])
    if f["status"] in ("Available", "Full", "Cancelled", "Completed"):
        sql += " AND s.status = ?"; params.append(f["status"])
    rows = db.execute(sql + " ORDER BY s.slot_date DESC, s.start_time", params).fetchall()
    exams_, examiners_ = _filter_choices(db)
    return render_template("admin/slots.html", slots=rows, f=f, exams=exams_, examiners=examiners_,
                           statuses=["Available", "Full", "Cancelled", "Completed"])


@admin_bp.route("/slots/<int:slot_id>/change-examiner", methods=["GET", "POST"])
@role_required("admin")
def change_examiner(slot_id):
    """Exceptional case: give a slot to another examiner of the same exam."""
    db = get_db()
    slot = db.execute(
        "SELECT s.*, e.exam_name, c.course_code, u.name AS examiner_name FROM slots s "
        "JOIN examinations e ON e.id = s.exam_id JOIN courses c ON c.id = e.course_id "
        "JOIN examiner_profiles p ON p.id = s.examiner_id JOIN users u ON u.id = p.user_id "
        "WHERE s.id = ?", (slot_id,)).fetchone()
    if slot is None:
        abort(404)
    if slot["status"] in ("Cancelled", "Completed"):
        flash("This slot is cancelled or completed, so its examiner cannot be changed.", "error")
        return redirect(url_for("admin.slots"))
    already_marked = db.execute(
        "SELECT COUNT(*) FROM evaluations ev JOIN bookings b ON b.id = ev.booking_id WHERE b.slot_id = ?",
        (slot_id,)).fetchone()[0]
    if already_marked:
        flash("Evaluations already exist for this slot, so its examiner cannot be changed.", "error")
        return redirect(url_for("admin.slots"))

    if request.method == "POST":
        new_id = _to_int(request.form.get("examiner_id", ""), 1)
        ok = db.execute(   # must be assigned to THIS exam and approved
            "SELECT 1 FROM exam_examiners x JOIN examiner_profiles p ON p.id = x.examiner_id "
            "JOIN users u ON u.id = p.user_id WHERE x.exam_id = ? AND x.examiner_id = ? AND u.status = 'Active'",
            (slot["exam_id"], new_id or 0)).fetchone()
        if not ok:
            flash("The new examiner must be an approved examiner assigned to this examination.", "error")
        elif new_id == slot["examiner_id"]:
            flash("That examiner already owns this slot.", "error")
        elif examiner_has_clash(db, new_id, slot["slot_date"], slot["start_time"], slot["end_time"], slot_id):
            flash("The new examiner has an overlapping slot at that time.", "error")
        else:
            db.execute("UPDATE slots SET examiner_id = ? WHERE id = ?", (new_id, slot_id))
            db.commit()
            flash("Slot examiner changed.", "success")
            return redirect(url_for("admin.slots"))
        return redirect(url_for("admin.change_examiner", slot_id=slot_id))

    candidates = db.execute(
        "SELECT p.id, u.name, p.department FROM exam_examiners x JOIN examiner_profiles p ON p.id = x.examiner_id "
        "JOIN users u ON u.id = p.user_id WHERE x.exam_id = ? AND u.status = 'Active' AND p.id != ? ORDER BY u.name",
        (slot["exam_id"], slot["examiner_id"])).fetchall()
    return render_template("admin/change_examiner.html", slot=slot, candidates=candidates)


@admin_bp.route("/bookings")
@role_required("admin")
def bookings():
    db = get_db()
    f = _filters()
    sql = ("SELECT b.*, s.slot_date, s.start_time, s.end_time, e.exam_name, c.course_code, su.name AS student_name, "
           " sp.roll_no, xu.name AS examiner_name "
           "FROM bookings b JOIN slots s ON s.id = b.slot_id JOIN examinations e ON e.id = b.exam_id "
           "JOIN courses c ON c.id = e.course_id JOIN users su ON su.id = b.student_id "
           "LEFT JOIN student_profiles sp ON sp.user_id = su.id "
           "JOIN examiner_profiles p ON p.id = s.examiner_id JOIN users xu ON xu.id = p.user_id WHERE 1=1")
    params = []
    if f["exam"].isdigit():
        sql += " AND b.exam_id = ?"; params.append(int(f["exam"]))
    if f["examiner"].isdigit():
        sql += " AND s.examiner_id = ?"; params.append(int(f["examiner"]))
    if f["date"]:
        sql += " AND s.slot_date = ?"; params.append(f["date"])
    if f["status"] in ("Booked", "Cancelled", "Completed"):
        sql += " AND b.status = ?"; params.append(f["status"])
    rows = db.execute(sql + " ORDER BY b.id DESC", params).fetchall()
    exams_, examiners_ = _filter_choices(db)
    return render_template("admin/bookings.html", bookings=rows, f=f, exams=exams_, examiners=examiners_,
                           statuses=["Booked", "Cancelled", "Completed"])


@admin_bp.route("/bookings/<int:booking_id>/reschedule", methods=["GET", "POST"])
@role_required("admin")
def reschedule(booking_id):
    db = get_db()
    b = db.execute(
        "SELECT b.*, su.name AS student_name, e.exam_name, s.slot_date, s.start_time, s.end_time "
        "FROM bookings b JOIN users su ON su.id = b.student_id JOIN examinations e ON e.id = b.exam_id "
        "JOIN slots s ON s.id = b.slot_id WHERE b.id = ?", (booking_id,)).fetchone()
    if b is None:
        abort(404)
    if request.method == "POST":
        ok, message = reschedule_booking(db, booking_id, _to_int(request.form.get("slot_id", ""), 1) or 0,
                                         session["user_id"])
        flash(message, "success" if ok else "error")
        if ok:
            return redirect(url_for("admin.bookings"))
        return redirect(url_for("admin.reschedule", booking_id=booking_id))
    if b["status"] != "Booked":
        flash("Only active bookings can be rescheduled.", "error")
        return redirect(url_for("admin.bookings"))
    options = db.execute(
        "SELECT s.*, u.name AS examiner_name FROM slots s JOIN examiner_profiles p ON p.id = s.examiner_id "
        "JOIN users u ON u.id = p.user_id WHERE s.exam_id = ? AND s.id != ? AND s.status = 'Available' "
        "AND s.slot_date >= ? ORDER BY s.slot_date, s.start_time",
        (b["exam_id"], b["slot_id"], today_iso())).fetchall()
    return render_template("admin/reschedule.html", b=b, options=options)


# ---------------------------------- search ----------------------------------

@admin_bp.route("/search")
@role_required("admin")
def search():
    """Search by name or ID across students, examiners, examinations and bookings."""
    q = request.args.get("q", "").strip()
    out = {"students": [], "examiners": [], "exams": [], "bookings": []}
    if q:
        db = get_db()
        like = f"%{q}%"
        num = int(q) if q.isdigit() else -1          # a number also matches IDs
        out["students"] = db.execute(
            "SELECT u.id, u.name, u.email, p.roll_no, p.program FROM users u "
            "JOIN student_profiles p ON p.user_id = u.id "
            "WHERE u.name LIKE ? OR u.email LIKE ? OR p.roll_no LIKE ? OR u.id = ? ORDER BY u.name",
            (like, like, like, num)).fetchall()
        out["examiners"] = db.execute(
            "SELECT u.id, u.name, u.email, u.status, p.department FROM users u "
            "JOIN examiner_profiles p ON p.user_id = u.id "
            "WHERE u.name LIKE ? OR u.email LIKE ? OR u.id = ? ORDER BY u.name", (like, like, num)).fetchall()
        out["exams"] = db.execute(
            "SELECT e.id, e.exam_name, e.status, c.course_code FROM examinations e "
            "JOIN courses c ON c.id = e.course_id "
            "WHERE e.exam_name LIKE ? OR c.course_code LIKE ? OR e.id = ? ORDER BY e.id DESC",
            (like, like, num)).fetchall()
        out["bookings"] = db.execute(
            "SELECT b.id, b.status, su.name AS student_name, sp.roll_no, e.exam_name, s.slot_date, s.start_time "
            "FROM bookings b JOIN users su ON su.id = b.student_id LEFT JOIN student_profiles sp ON sp.user_id = su.id "
            "JOIN examinations e ON e.id = b.exam_id JOIN slots s ON s.id = b.slot_id "
            "WHERE b.id = ? OR su.name LIKE ? OR sp.roll_no LIKE ? OR e.exam_name LIKE ? ORDER BY b.id DESC",
            (num, like, like, like)).fetchall()
    return render_template("admin/search.html", q=q, r=out)
