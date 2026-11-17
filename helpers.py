# helpers.py - decorators (and later: slot/booking helper functions)
from functools import wraps

from flask import abort, flash, redirect, session, url_for


def login_required(view):
    """Allow the page only if a user is logged in."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    """Allow the page only for the given role(s). Wrong role -> 403."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if session.get("role") not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# Dates and exam status automation
# ---------------------------------------------------------------------------
from datetime import date


def today_iso():
    """Today's date as YYYY-MM-DD text (same format as stored in the DB)."""
    return date.today().isoformat()


def compute_exam_status(db, exam):
    """Work out the status an exam should have today, from its four dates.

    ISO date strings compare correctly as plain text, so no parsing is needed.
      today > booking_end            -> Completed (all slots are in the past) or Closed
      today >= booking_start         -> Booking Open
      today >= slot_creation_start   -> Slot Creation (also covers the gap before booking)
      otherwise                      -> Draft
    """
    t = today_iso()
    if t > exam["booking_end"]:
        last = db.execute(
            "SELECT MAX(slot_date) FROM slots WHERE exam_id = ? AND status != 'Cancelled'",
            (exam["id"],),
        ).fetchone()[0]
        return "Completed" if last and last < t else "Closed"
    if t >= exam["booking_start"]:
        return "Booking Open"
    if t >= exam["slot_creation_start"]:
        return "Slot Creation"
    return "Draft"


def refresh_exam_status(db, exam):
    """Update one exam's status unless the admin manually overrode it.
    The caller commits. Returns the (possibly new) status."""
    if exam["status_override"]:
        return exam["status"]
    new_status = compute_exam_status(db, exam)
    if new_status != exam["status"]:
        db.execute("UPDATE examinations SET status = ? WHERE id = ?", (new_status, exam["id"]))
    return new_status


def refresh_all_exams(db):
    """Refresh every exam (there are only a few, so this is cheap)."""
    for exam in db.execute("SELECT * FROM examinations").fetchall():
        refresh_exam_status(db, exam)
    db.commit()


# ---------------------------------------------------------------------------
# Time, window and slot helpers (Phase 4)
# ---------------------------------------------------------------------------
import re
import sqlite3

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def valid_time(text):
    """True for 'HH:MM' (24-hour)."""
    return bool(_TIME_RE.match(text or ""))


def minutes(hhmm):
    """'09:30' -> 570 (minutes since midnight)."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def parse_date(text):
    """Return the ISO date text if valid, else None."""
    try:
        return date.fromisoformat((text or "").strip()).isoformat()
    except ValueError:
        return None


def slot_window_open(exam):
    """Business rule 1: slots can be created only inside the creation window."""
    return exam["slot_creation_start"] <= today_iso() <= exam["slot_creation_end"]


def booking_window_open(exam):
    """Business rule 2: bookings are allowed only inside the booking window."""
    return exam["booking_start"] <= today_iso() <= exam["booking_end"]


def examiner_profile_id(db, user_id):
    """examiner_profiles.id for a logged-in examiner user (None if missing)."""
    row = db.execute("SELECT id FROM examiner_profiles WHERE user_id = ?", (user_id,)).fetchone()
    return row["id"] if row else None


def examiner_has_clash(db, examiner_id, slot_date, start, end, ignore_slot_id=0):
    """Business rule 7: True if the examiner already has an overlapping slot.
    Two slots overlap when they are on the same date and each starts before the other ends.
    HH:MM text compares correctly as plain text."""
    row = db.execute(
        "SELECT 1 FROM slots WHERE examiner_id = ? AND slot_date = ? AND status != 'Cancelled' "
        "AND id != ? AND start_time < ? AND ? < end_time",
        (examiner_id, slot_date, ignore_slot_id, end, start)).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Booking transactions (Phase 5)
# ---------------------------------------------------------------------------
class BookingError(Exception):
    """A business rule stopped the booking/cancellation (message is shown to the user)."""


def book_slot(db, student_id, slot_id):
    """Book one seat. Returns (ok, message).

    Everything runs in ONE transaction. BEGIN IMMEDIATE takes SQLite's write lock first,
    so two students can never read the same 'last seat' at the same time.
    """
    try:
        db.execute("BEGIN IMMEDIATE")
        slot = db.execute(
            "SELECT s.*, e.booking_start, e.booking_end FROM slots s "
            "JOIN examinations e ON e.id = s.exam_id WHERE s.id = ?", (slot_id,)).fetchone()
        if slot is None:
            raise BookingError("Slot not found.")
        t = today_iso()
        if not (slot["booking_start"] <= t <= slot["booking_end"]):          # rule 2
            raise BookingError("Booking is not open for this examination.")
        if slot["status"] in ("Cancelled", "Completed") or slot["slot_date"] < t:
            raise BookingError("This slot is no longer available.")
        if db.execute("SELECT 1 FROM bookings WHERE student_id = ? AND exam_id = ? "
                      "AND status IN ('Booked', 'Completed')",
                      (student_id, slot["exam_id"])).fetchone():              # rule 5
            raise BookingError("You already have an active booking for this examination.")
        if db.execute(                                                        # rule 6
                "SELECT 1 FROM bookings b JOIN slots s ON s.id = b.slot_id "
                "WHERE b.student_id = ? AND b.status = 'Booked' AND s.slot_date = ? "
                "AND s.start_time < ? AND ? < s.end_time",
                (student_id, slot["slot_date"], slot["end_time"], slot["start_time"])).fetchone():
            raise BookingError("This slot overlaps with another slot you have booked.")

        # Rule 3: the guard 'available_seats > 0' is inside the UPDATE itself
        cur = db.execute("UPDATE slots SET available_seats = available_seats - 1 "
                         "WHERE id = ? AND available_seats > 0", (slot_id,))
        if cur.rowcount == 0:
            raise BookingError("Sorry, this slot is full.")
        # Rule 4: last seat taken -> Full
        db.execute("UPDATE slots SET status = 'Full' WHERE id = ? AND available_seats = 0", (slot_id,))

        cur = db.execute("INSERT INTO bookings (student_id, slot_id, exam_id) VALUES (?, ?, ?)",
                         (student_id, slot_id, slot["exam_id"]))
        db.execute("INSERT INTO booking_history (booking_id, action, new_slot_id, acted_by) "
                   "VALUES (?, 'Booked', ?, ?)", (cur.lastrowid, slot_id, student_id))
        db.commit()
        return True, "Slot booked successfully."
    except BookingError as e:
        db.rollback()
        return False, str(e)
    except sqlite3.Error:
        db.rollback()
        return False, "Could not complete the booking. Please try again."


def cancel_booking(db, booking_id, student_id):
    """Cancel a student's own booking and give the seat back. Returns (ok, message)."""
    try:
        db.execute("BEGIN IMMEDIATE")
        b = db.execute(
            "SELECT b.*, e.booking_end FROM bookings b JOIN examinations e ON e.id = b.exam_id "
            "WHERE b.id = ? AND b.student_id = ?", (booking_id, student_id)).fetchone()
        if b is None:
            raise BookingError("Booking not found.")
        if b["status"] != "Booked":
            raise BookingError("This booking is not active.")
        if today_iso() > b["booking_end"]:
            raise BookingError("The booking period has ended, so this booking can no longer be cancelled.")

        db.execute("UPDATE bookings SET status = 'Cancelled', cancelled_at = datetime('now') WHERE id = ?",
                   (booking_id,))
        # Seat returns; a Full slot becomes Available again
        db.execute("UPDATE slots SET available_seats = available_seats + 1, "
                   "status = CASE WHEN status = 'Full' THEN 'Available' ELSE status END WHERE id = ?",
                   (b["slot_id"],))
        db.execute("INSERT INTO booking_history (booking_id, action, old_slot_id, acted_by) "
                   "VALUES (?, 'Cancelled', ?, ?)", (booking_id, b["slot_id"], student_id))
        db.commit()
        return True, "Booking cancelled. The seat is available again."
    except BookingError as e:
        db.rollback()
        return False, str(e)
    except sqlite3.Error:
        db.rollback()
        return False, "Could not cancel the booking. Please try again."


def reschedule_booking(db, booking_id, new_slot_id, admin_id):
    """Admin moves an active booking to another slot of the SAME exam. Returns (ok, message).
    One transaction: seat back on the old slot, seat taken on the new one, history row written."""
    try:
        db.execute("BEGIN IMMEDIATE")
        b = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if b is None or b["status"] != "Booked":
            raise BookingError("Only active bookings can be rescheduled.")
        new = db.execute("SELECT * FROM slots WHERE id = ?", (new_slot_id,)).fetchone()
        if new is None or new["exam_id"] != b["exam_id"]:
            raise BookingError("Choose a slot of the same examination.")
        if new["id"] == b["slot_id"]:
            raise BookingError("The booking is already in this slot.")
        if new["status"] != "Available" or new["slot_date"] < today_iso():
            raise BookingError("That slot is not available.")
        if db.execute(   # the student must not clash with their OTHER bookings
                "SELECT 1 FROM bookings x JOIN slots s ON s.id = x.slot_id "
                "WHERE x.student_id = ? AND x.status = 'Booked' AND x.id != ? AND s.slot_date = ? "
                "AND s.start_time < ? AND ? < s.end_time",
                (b["student_id"], booking_id, new["slot_date"], new["end_time"], new["start_time"])).fetchone():
            raise BookingError("The new slot overlaps another booking of this student.")

        cur = db.execute("UPDATE slots SET available_seats = available_seats - 1 "
                         "WHERE id = ? AND available_seats > 0", (new_slot_id,))
        if cur.rowcount == 0:
            raise BookingError("That slot is full.")
        db.execute("UPDATE slots SET status = 'Full' WHERE id = ? AND available_seats = 0", (new_slot_id,))
        db.execute("UPDATE slots SET available_seats = available_seats + 1, "
                   "status = CASE WHEN status = 'Full' THEN 'Available' ELSE status END WHERE id = ?",
                   (b["slot_id"],))
        db.execute("UPDATE bookings SET slot_id = ?, rescheduled_from_slot_id = ? WHERE id = ?",
                   (new_slot_id, b["slot_id"], booking_id))
        db.execute("INSERT INTO booking_history (booking_id, action, old_slot_id, new_slot_id, acted_by) "
                   "VALUES (?, 'Rescheduled', ?, ?, ?)", (booking_id, b["slot_id"], new_slot_id, admin_id))
        db.commit()
        return True, "Booking rescheduled. Seat counts updated on both slots."
    except BookingError as e:
        db.rollback()
        return False, str(e)
    except sqlite3.Error:
        db.rollback()
        return False, "Could not reschedule. Please try again."
