# routes/student.py - all /student routes
# Phase 5: dashboard, browse/search exams, slots, book, cancel, bookings + history, profile
from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_db
from helpers import (book_slot, booking_window_open, cancel_booking,
                     role_required, today_iso)

student_bp = Blueprint("student", __name__)

EXAM_TYPES = ["Viva", "Practical", "Project Demo", "Assessment"]
VISIBLE_STATUSES = ["Slot Creation", "Booking Open", "Closed", "Completed"]   # students never see Draft


@student_bp.route("/dashboard")
@role_required("student")
def dashboard():
    db = get_db()
    uid, t = session["user_id"], today_iso()
    one = lambda sql, *p: db.execute(sql, p).fetchone()[0]
    stats = {
        "Open examinations": one("SELECT COUNT(*) FROM examinations WHERE ? BETWEEN booking_start AND booking_end", t),
        "Available slots": one(
            "SELECT COUNT(*) FROM slots s JOIN examinations e ON e.id = s.exam_id "
            "WHERE ? BETWEEN e.booking_start AND e.booking_end AND s.status = 'Available' AND s.slot_date >= ?", t, t),
        "Upcoming examinations": one(
            "SELECT COUNT(*) FROM bookings b JOIN slots s ON s.id = b.slot_id "
            "WHERE b.student_id = ? AND b.status = 'Booked' AND s.slot_date >= ?", uid, t),
        "Booked slots": one("SELECT COUNT(*) FROM bookings WHERE student_id = ? AND status = 'Booked'", uid),
        "Published results": one(
            "SELECT COUNT(*) FROM evaluations ev JOIN bookings b ON b.id = ev.booking_id "
            "WHERE b.student_id = ? AND ev.is_published = 1", uid),
    }
    upcoming = db.execute(
        "SELECT s.slot_date, s.start_time, s.end_time, s.location, e.exam_name, c.course_code "
        "FROM bookings b JOIN slots s ON s.id = b.slot_id JOIN examinations e ON e.id = b.exam_id "
        "JOIN courses c ON c.id = e.course_id "
        "WHERE b.student_id = ? AND b.status = 'Booked' AND s.slot_date >= ? "
        "ORDER BY s.slot_date, s.start_time LIMIT 5", (uid, t)).fetchall()
    return render_template("student/dashboard.html", stats=stats, upcoming=upcoming)


@student_bp.route("/exams")
@role_required("student")
def exams():
    """Browse + search (name/code) + filter (course, type, status) using the query string."""
    db = get_db()
    q = request.args.get("q", "").strip()
    course = request.args.get("course", "").strip()
    etype = request.args.get("type", "").strip()
    status = request.args.get("status", "").strip()

    sql = ("SELECT e.*, c.course_code, c.course_name, "
           " (SELECT b.id FROM bookings b WHERE b.exam_id = e.id AND b.student_id = ? "
           "  AND b.status = 'Booked') AS my_booking "
           "FROM examinations e JOIN courses c ON c.id = e.course_id WHERE e.status != 'Draft'")
    params = [session["user_id"]]
    if q:
        sql += " AND (e.exam_name LIKE ? OR c.course_code LIKE ? OR c.course_name LIKE ?)"
        params += [f"%{q}%"] * 3
    if course.isdigit():
        sql += " AND e.course_id = ?"
        params.append(int(course))
    if etype in EXAM_TYPES:
        sql += " AND e.exam_type = ?"
        params.append(etype)
    if status in VISIBLE_STATUSES:
        sql += " AND e.status = ?"
        params.append(status)
    rows = db.execute(sql + " ORDER BY e.booking_start DESC, e.id DESC", params).fetchall()
    courses = db.execute("SELECT id, course_code, course_name FROM courses ORDER BY course_code").fetchall()
    return render_template("student/exams.html", exams=rows, courses=courses, types=EXAM_TYPES,
                           statuses=VISIBLE_STATUSES, q=q, course=course, etype=etype, status=status)


@student_bp.route("/exams/<int:exam_id>/slots")
@role_required("student")
def exam_slots(exam_id):
    db = get_db()
    exam = db.execute("SELECT e.*, c.course_code, c.course_name FROM examinations e "
                      "JOIN courses c ON c.id = e.course_id WHERE e.id = ? AND e.status != 'Draft'",
                      (exam_id,)).fetchone()
    if exam is None:
        abort(404)
    slots = db.execute(
        "SELECT s.*, u.name AS examiner_name FROM slots s "
        "JOIN examiner_profiles p ON p.id = s.examiner_id JOIN users u ON u.id = p.user_id "
        "WHERE s.exam_id = ? AND s.status != 'Cancelled' ORDER BY s.slot_date, s.start_time",
        (exam_id,)).fetchall()
    mine = db.execute("SELECT * FROM bookings WHERE student_id = ? AND exam_id = ? AND status = 'Booked'",
                      (session["user_id"], exam_id)).fetchone()
    return render_template("student/book.html", exam=exam, slots=slots, mine=mine,
                           window_open=booking_window_open(exam), today=today_iso())


@student_bp.route("/book/<int:slot_id>", methods=["POST"])
@role_required("student")
def book(slot_id):
    db = get_db()
    slot = db.execute("SELECT exam_id FROM slots WHERE id = ?", (slot_id,)).fetchone()
    if slot is None:
        abort(404)
    ok, message = book_slot(db, session["user_id"], slot_id)   # transaction lives in helpers.py
    flash(message, "success" if ok else "error")
    return redirect(url_for("student.bookings" if ok else "student.exam_slots", exam_id=slot["exam_id"]))


@student_bp.route("/cancel/<int:booking_id>", methods=["GET", "POST"])
@role_required("student")
def cancel(booking_id):
    db = get_db()
    b = db.execute(
        "SELECT b.*, s.slot_date, s.start_time, s.end_time, e.exam_name FROM bookings b "
        "JOIN slots s ON s.id = b.slot_id JOIN examinations e ON e.id = b.exam_id WHERE b.id = ?",
        (booking_id,)).fetchone()
    if b is None:
        abort(404)
    if b["student_id"] != session["user_id"]:
        abort(403)                                   # never touch another student's booking
    if request.method == "POST":
        ok, message = cancel_booking(db, booking_id, session["user_id"])
        flash(message, "success" if ok else "error")
        return redirect(url_for("student.bookings"))
    return render_template("confirm_action.html", title="Cancel booking", button="Yes, cancel booking",
                           message=f"Cancel your booking for {b['exam_name']} on {b['slot_date']} "
                                   f"({b['start_time']}-{b['end_time']})?",
                           cancel_url=url_for("student.bookings"))


@student_bp.route("/bookings")
@role_required("student")
def bookings():
    db = get_db()
    uid, t = session["user_id"], today_iso()
    rows = db.execute(
        "SELECT b.*, s.slot_date, s.start_time, s.end_time, s.location, e.exam_name, e.booking_end, "
        " c.course_code, u.name AS examiner_name "
        "FROM bookings b JOIN slots s ON s.id = b.slot_id JOIN examinations e ON e.id = b.exam_id "
        "JOIN courses c ON c.id = e.course_id JOIN examiner_profiles p ON p.id = s.examiner_id "
        "JOIN users u ON u.id = p.user_id WHERE b.student_id = ? "
        "ORDER BY s.slot_date DESC, s.start_time DESC", (uid,)).fetchall()
    rows = [dict(r, can_cancel=(r["status"] == "Booked" and t <= r["booking_end"])) for r in rows]
    history = db.execute(
        "SELECT h.*, e.exam_name, os.slot_date AS old_date, os.start_time AS old_start, "
        " ns.slot_date AS new_date, ns.start_time AS new_start, a.name AS actor, a.role AS actor_role "
        "FROM booking_history h JOIN bookings b ON b.id = h.booking_id "
        "JOIN examinations e ON e.id = b.exam_id LEFT JOIN slots os ON os.id = h.old_slot_id "
        "LEFT JOIN slots ns ON ns.id = h.new_slot_id LEFT JOIN users a ON a.id = h.acted_by "
        "WHERE b.student_id = ? ORDER BY h.acted_at DESC, h.id DESC", (uid,)).fetchall()
    return render_template("student/bookings.html", bookings=rows, history=history)


@student_bp.route("/profile", methods=["GET", "POST"])
@role_required("student")
def profile():
    db = get_db()
    uid = session["user_id"]
    row = db.execute("SELECT u.*, p.roll_no, p.program, p.year FROM users u "
                     "JOIN student_profiles p ON p.user_id = u.id WHERE u.id = ?", (uid,)).fetchone()
    if request.method == "POST":
        f = request.form
        name, phone, program = f.get("name", "").strip(), f.get("phone", "").strip(), f.get("program", "").strip()
        current, new, confirm = f.get("current_password", ""), f.get("new_password", ""), f.get("confirm_password", "")
        errors = []
        if not name:
            errors.append("Name is required.")
        if not program:
            errors.append("Program is required.")
        if new or confirm or current:                     # password change is optional
            if not check_password_hash(row["password_hash"], current):
                errors.append("Current password is incorrect.")
            if len(new) < 6:
                errors.append("New password must be at least 6 characters.")
            if new != confirm:
                errors.append("New passwords do not match.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("student/profile.html", p=row, form=f)
        db.execute("UPDATE users SET name = ?, phone = ? WHERE id = ?", (name, phone, uid))
        db.execute("UPDATE student_profiles SET program = ? WHERE user_id = ?", (program, uid))
        if new:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), uid))
        db.commit()
        session["name"] = name
        flash("Profile updated.", "success")
        return redirect(url_for("student.profile"))
    return render_template("student/profile.html", p=row, form=dict(row))


@student_bp.route("/results")
@role_required("student")
def results():
    """Only evaluations with is_published = 1 are ever selected (business rule 11)."""
    db = get_db()
    evals = db.execute(
        "SELECT ev.*, e.exam_name, e.max_marks, c.course_code, c.course_name, s.slot_date, xu.name AS examiner_name "
        "FROM evaluations ev JOIN bookings b ON b.id = ev.booking_id JOIN slots s ON s.id = b.slot_id "
        "JOIN examinations e ON e.id = b.exam_id JOIN courses c ON c.id = e.course_id "
        "JOIN examiner_profiles p ON p.id = ev.examiner_id JOIN users xu ON xu.id = p.user_id "
        "WHERE b.student_id = ? AND ev.is_published = 1 ORDER BY ev.evaluated_at DESC",
        (session["user_id"],)).fetchall()
    results_ = []
    for ev in evals:
        scores = db.execute(
            "SELECT r.criterion_name, r.max_marks, sc.marks_awarded FROM evaluation_scores sc "
            "JOIN rubrics r ON r.id = sc.rubric_id WHERE sc.evaluation_id = ? ORDER BY r.id", (ev["id"],)).fetchall()
        pct = round(ev["total_marks"] / ev["max_marks"] * 100, 1) if ev["max_marks"] else 0
        results_.append({"ev": ev, "scores": scores, "pct": pct})
    return render_template("student/results.html", results=results_)
