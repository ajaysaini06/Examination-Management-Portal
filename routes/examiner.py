# routes/examiner.py - all /examiner routes (Phase 6 adds evaluation)
# Phase 4: dashboard, assigned exams, slot create/edit/delete, booked students
import math

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, session, url_for)

from database import get_db
from helpers import (examiner_has_clash, examiner_profile_id, minutes,
                     parse_date, role_required, slot_window_open, today_iso,
                     valid_time)

examiner_bp = Blueprint("examiner", __name__)


def _profile_id(db):
    """The logged-in examiner's profile id (403 if the profile is missing)."""
    pid = examiner_profile_id(db, session["user_id"])
    if pid is None:
        abort(403)
    return pid


def _assigned_exams(db, pid):
    """Exams the admin assigned to this examiner."""
    return db.execute(
        "SELECT e.*, c.course_code, c.course_name, "
        " (SELECT COUNT(*) FROM slots s WHERE s.exam_id = e.id AND s.examiner_id = x.examiner_id) AS my_slots "
        "FROM exam_examiners x JOIN examinations e ON e.id = x.exam_id "
        "JOIN courses c ON c.id = e.course_id WHERE x.examiner_id = ? ORDER BY e.id DESC", (pid,)).fetchall()


def _own_slot_or_abort(db, slot_id, pid):
    """Load a slot with its exam. 404 if missing, 403 if it belongs to another examiner."""
    slot = db.execute(
        "SELECT s.*, e.exam_name, e.duration_minutes, e.booking_start, e.venue, e.max_marks, c.course_code "
        "FROM slots s JOIN examinations e ON e.id = s.exam_id JOIN courses c ON c.id = e.course_id "
        "WHERE s.id = ?", (slot_id,)).fetchone()
    if slot is None:
        abort(404)
    if slot["examiner_id"] != pid:
        abort(403)
    return slot


def _lock_reason(db, slot):
    """Edit/delete only before booking starts AND only if the slot has no bookings.
    Returns an error message, or None if the slot may be changed."""
    if today_iso() >= slot["booking_start"]:
        return "Booking has already started for this examination, so the slot can no longer be changed."
    if db.execute("SELECT COUNT(*) FROM bookings WHERE slot_id = ?", (slot["id"],)).fetchone()[0]:
        return "This slot already has bookings, so it cannot be changed."
    return None


def _read_slot_form(db, f, pid, exam, slot_id=0):
    """Validate date, times, capacity and examiner clash. Returns (clean_dict, errors)."""
    errors = []
    slot_date = parse_date(f.get("slot_date", ""))
    start = f.get("start_time", "").strip()
    end = f.get("end_time", "").strip()
    try:
        capacity = int(f.get("capacity", "").strip())
    except ValueError:
        capacity = 0
    location = f.get("location", "").strip() or (exam["venue"] or "")

    if slot_date is None:
        errors.append("Enter a valid slot date.")
    elif slot_date < today_iso():
        errors.append("The slot date cannot be in the past.")
    elif slot_date < exam["booking_start"]:
        errors.append(f"The slot date cannot be before booking opens ({exam['booking_start']}).")

    times_ok = valid_time(start) and valid_time(end)
    if not times_ok:
        errors.append("Enter valid start and end times.")
    elif minutes(end) <= minutes(start):                                    # rule 8
        errors.append("End time must be after the start time.")
    elif minutes(end) - minutes(start) < exam["duration_minutes"]:          # rule 8
        errors.append(f"The slot must be at least {exam['duration_minutes']} minutes "
                      "(the examination duration).")

    if not 1 <= capacity <= 100:
        errors.append("Capacity must be a whole number between 1 and 100.")

    if not errors and examiner_has_clash(db, pid, slot_date, start, end, slot_id):   # rule 7
        errors.append("You already have a slot that overlaps this date and time.")

    return {"slot_date": slot_date, "start_time": start, "end_time": end,
            "capacity": capacity, "location": location}, errors


# ------------------------------- dashboard -------------------------------

@examiner_bp.route("/dashboard")
@role_required("examiner")
def dashboard():
    db = get_db()
    pid = _profile_id(db)
    one = lambda sql, *p: db.execute(sql, p).fetchone()[0]
    stats = {
        "Assigned examinations": one("SELECT COUNT(*) FROM exam_examiners WHERE examiner_id = ?", pid),
        "Slots created": one("SELECT COUNT(*) FROM slots WHERE examiner_id = ?", pid),
        "Booked students": one("SELECT COUNT(*) FROM bookings b JOIN slots s ON s.id = b.slot_id "
                               "WHERE s.examiner_id = ? AND b.status = 'Booked'", pid),
        # booked, slot date has arrived, and no submitted evaluation yet
        "Pending evaluations": one(
            "SELECT COUNT(*) FROM bookings b JOIN slots s ON s.id = b.slot_id "
            "LEFT JOIN evaluations ev ON ev.booking_id = b.id "
            "WHERE s.examiner_id = ? AND b.status = 'Booked' AND s.slot_date <= ? "
            "AND (ev.id IS NULL OR ev.status = 'Pending')", pid, today_iso()),
    }
    return render_template("examiner/dashboard.html", stats=stats,
                           exams=_assigned_exams(db, pid), today=today_iso())


@examiner_bp.route("/exams")
@role_required("examiner")
def exams():
    db = get_db()
    rows = [dict(e, can_create=slot_window_open(e)) for e in _assigned_exams(db, _profile_id(db))]
    return render_template("examiner/exams.html", exams=rows)


# --------------------------------- slots ---------------------------------

@examiner_bp.route("/slots")
@role_required("examiner")
def slots():
    db = get_db()
    pid = _profile_id(db)
    rows = db.execute(
        "SELECT s.*, e.exam_name, e.booking_start, c.course_code, "
        " (SELECT COUNT(*) FROM bookings b WHERE b.slot_id = s.id) AS booking_rows "
        "FROM slots s JOIN examinations e ON e.id = s.exam_id JOIN courses c ON c.id = e.course_id "
        "WHERE s.examiner_id = ? ORDER BY s.slot_date, s.start_time", (pid,)).fetchall()
    t = today_iso()
    slots_ = [dict(s, editable=(t < s["booking_start"] and s["booking_rows"] == 0)) for s in rows]
    return render_template("examiner/slots.html", slots=slots_)


@examiner_bp.route("/slots/new", methods=["GET", "POST"])
@role_required("examiner")
def new_slot():
    db = get_db()
    pid = _profile_id(db)
    exams_ = _assigned_exams(db, pid)

    if request.method == "POST":
        f = request.form
        exam = next((e for e in exams_ if str(e["id"]) == f.get("exam_id")), None)
        errors = []
        if exam is None:
            errors.append("Choose an examination that is assigned to you.")
        elif not slot_window_open(exam):                                     # rule 1
            errors.append(f"Slot creation is not open for this examination "
                          f"(open {exam['slot_creation_start']} to {exam['slot_creation_end']}).")
        data = {}
        if exam is not None and not errors:
            data, errors = _read_slot_form(db, f, pid, exam)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("examiner/slot_form.html", exams=exams_, form=f, editing=False)
        db.execute(
            "INSERT INTO slots (exam_id, examiner_id, slot_date, start_time, end_time, capacity, "
            " available_seats, location, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Available')",
            (exam["id"], pid, data["slot_date"], data["start_time"], data["end_time"],
             data["capacity"], data["capacity"], data["location"]))
        db.commit()
        flash("Slot created.", "success")
        return redirect(url_for("examiner.slots"))

    return render_template("examiner/slot_form.html", exams=exams_,
                           form={"exam_id": request.args.get("exam_id", "")}, editing=False)


@examiner_bp.route("/slots/<int:slot_id>/edit", methods=["GET", "POST"])
@role_required("examiner")
def edit_slot(slot_id):
    db = get_db()
    pid = _profile_id(db)
    slot = _own_slot_or_abort(db, slot_id, pid)
    reason = _lock_reason(db, slot)
    if reason:
        flash(reason, "error")
        return redirect(url_for("examiner.slots"))

    if request.method == "POST":
        data, errors = _read_slot_form(db, request.form, pid, slot, slot_id)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("examiner/slot_form.html", slot=slot, form=request.form, editing=True)
        db.execute(
            "UPDATE slots SET slot_date=?, start_time=?, end_time=?, capacity=?, available_seats=?, "
            "location=?, status='Available' WHERE id=?",
            (data["slot_date"], data["start_time"], data["end_time"], data["capacity"],
             data["capacity"], data["location"], slot_id))   # no bookings, so all seats are free
        db.commit()
        flash("Slot updated.", "success")
        return redirect(url_for("examiner.slots"))
    return render_template("examiner/slot_form.html", slot=slot, form=dict(slot), editing=True)


@examiner_bp.route("/slots/<int:slot_id>/delete", methods=["GET", "POST"])
@role_required("examiner")
def delete_slot(slot_id):
    db = get_db()
    slot = _own_slot_or_abort(db, slot_id, _profile_id(db))
    reason = _lock_reason(db, slot)
    if reason:
        flash(reason, "error")
        return redirect(url_for("examiner.slots"))
    if request.method == "POST":
        db.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        db.commit()
        flash("Slot deleted.", "success")
        return redirect(url_for("examiner.slots"))
    return render_template("confirm_action.html", title="Confirm deletion", button="Yes, delete",
                           message=f"Delete the slot on {slot['slot_date']} {slot['start_time']}-{slot['end_time']} "
                                   f"for {slot['exam_name']}?",
                           cancel_url=url_for("examiner.slots"))


@examiner_bp.route("/slots/<int:slot_id>/students")
@role_required("examiner")
def slot_students(slot_id):
    db = get_db()
    slot = _own_slot_or_abort(db, slot_id, _profile_id(db))   # other examiners' slots -> 403
    students = db.execute(
        "SELECT b.id AS booking_id, b.status, b.booking_date, u.name, u.email, sp.roll_no, sp.program, "
        " ev.total_marks, ev.status AS eval_status, ev.is_published "
        "FROM bookings b JOIN users u ON u.id = b.student_id "
        "LEFT JOIN student_profiles sp ON sp.user_id = u.id "
        "LEFT JOIN evaluations ev ON ev.booking_id = b.id "
        "WHERE b.slot_id = ? ORDER BY b.status, u.name", (slot_id,)).fetchall()
    return render_template("examiner/booked_students.html", slot=slot, students=students,
                           can_evaluate=slot["slot_date"] <= today_iso())


# ------------------------------- evaluation -------------------------------

def _read_marks(f, rubrics):
    """One marks input per rubric criterion. Returns ({rubric_id: marks}, errors).
    Business rule 9: marks for a criterion can never exceed that criterion's maximum."""
    marks, errors = {}, []
    for r in rubrics:
        raw = f.get(f"marks_{r['id']}", "").strip()
        try:
            value = float(raw)
        except ValueError:
            errors.append(f"Enter marks for \"{r['criterion_name']}\".")
            continue
        if not math.isfinite(value) or value < 0 or value > r["max_marks"]:
            errors.append(f"Marks for \"{r['criterion_name']}\" must be between 0 and {r['max_marks']:g}.")
        else:
            marks[r["id"]] = round(value, 2)
    return marks, errors


@examiner_bp.route("/evaluate/<int:booking_id>", methods=["GET", "POST"])
@role_required("examiner")
def evaluate(booking_id):
    db = get_db()
    pid = _profile_id(db)
    b = db.execute(
        "SELECT b.*, s.slot_date, s.start_time, s.end_time, s.examiner_id, e.exam_name, e.max_marks, "
        " c.course_code, u.name AS student_name, sp.roll_no "
        "FROM bookings b JOIN slots s ON s.id = b.slot_id JOIN examinations e ON e.id = b.exam_id "
        "JOIN courses c ON c.id = e.course_id JOIN users u ON u.id = b.student_id "
        "LEFT JOIN student_profiles sp ON sp.user_id = u.id WHERE b.id = ?", (booking_id,)).fetchone()
    if b is None:
        abort(404)
    if b["examiner_id"] != pid:          # rule 10: only students in my own slots -> 403 otherwise
        abort(403)
    back = url_for("examiner.slot_students", slot_id=b["slot_id"])
    if b["status"] == "Cancelled":
        flash("This booking was cancelled, so it cannot be evaluated.", "error")
        return redirect(back)
    if b["slot_date"] > today_iso():     # rule 10: only once the slot date has arrived
        flash(f"You can evaluate only once the slot date has arrived ({b['slot_date']}).", "error")
        return redirect(back)

    rubrics = db.execute("SELECT * FROM rubrics WHERE exam_id = ? ORDER BY id", (b["exam_id"],)).fetchall()
    if not rubrics:
        flash("No rubric criteria are defined for this examination. Ask the admin to add them.", "error")
        return redirect(back)
    ev = db.execute("SELECT * FROM evaluations WHERE booking_id = ?", (booking_id,)).fetchone()
    if ev and ev["is_published"]:
        flash("These results are already published, so they can no longer be changed.", "error")
        return redirect(back)

    if request.method == "POST":
        marks, errors = _read_marks(request.form, rubrics)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("examiner/evaluate.html", b=b, rubrics=rubrics, form=request.form, ev=ev)
        total = round(sum(marks.values()), 2)          # total is always calculated on the server
        remarks = request.form.get("remarks", "").strip()
        if ev:
            db.execute("UPDATE evaluations SET total_marks = ?, remarks = ?, status = 'Submitted', "
                       "evaluated_at = datetime('now') WHERE id = ?", (total, remarks, ev["id"]))
            db.execute("DELETE FROM evaluation_scores WHERE evaluation_id = ?", (ev["id"],))
            eval_id = ev["id"]
        else:
            cur = db.execute("INSERT INTO evaluations (booking_id, examiner_id, total_marks, remarks, status, "
                             "evaluated_at) VALUES (?, ?, ?, ?, 'Submitted', datetime('now'))",
                             (booking_id, pid, total, remarks))
            eval_id = cur.lastrowid
        for rubric_id, value in marks.items():
            db.execute("INSERT INTO evaluation_scores (evaluation_id, rubric_id, marks_awarded) VALUES (?, ?, ?)",
                       (eval_id, rubric_id, value))
        db.execute("UPDATE bookings SET status = 'Completed' WHERE id = ?", (booking_id,))
        db.commit()
        flash(f"Evaluation submitted. Total: {total:g}.", "success")
        return redirect(back)

    form = {}
    if ev:
        form = {f"marks_{s['rubric_id']}": s["marks_awarded"] for s in
                db.execute("SELECT * FROM evaluation_scores WHERE evaluation_id = ?", (ev["id"],))}
        form["remarks"] = ev["remarks"] or ""
    return render_template("examiner/evaluate.html", b=b, rubrics=rubrics, form=form, ev=ev)
