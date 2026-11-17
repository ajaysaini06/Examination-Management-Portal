-- schema.sql : all tables for EMP. Dates are ISO text (YYYY-MM-DD, HH:MM).

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'examiner', 'student')),
    phone         TEXT,
    status        TEXT NOT NULL DEFAULT 'Active'
                  CHECK (status IN ('Active', 'Pending', 'Deactivated')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS student_profiles (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    roll_no TEXT NOT NULL UNIQUE,
    program TEXT,
    year    INTEGER
);

CREATE TABLE IF NOT EXISTS examiner_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL UNIQUE REFERENCES users(id),
    department      TEXT,
    designation     TEXT,
    contact_details TEXT
);

CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_code TEXT NOT NULL UNIQUE,
    course_name TEXT NOT NULL,
    description TEXT,
    credits     INTEGER,
    status      TEXT NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'Inactive'))
);

CREATE TABLE IF NOT EXISTS examinations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id           INTEGER NOT NULL REFERENCES courses(id),
    exam_name           TEXT NOT NULL,
    exam_type           TEXT NOT NULL
                        CHECK (exam_type IN ('Viva', 'Practical', 'Project Demo', 'Assessment')),
    duration_minutes    INTEGER NOT NULL,
    max_marks           INTEGER NOT NULL,
    slot_creation_start TEXT NOT NULL,
    slot_creation_end   TEXT NOT NULL,
    booking_start       TEXT NOT NULL,
    booking_end         TEXT NOT NULL,
    venue               TEXT,
    instructions        TEXT,
    status              TEXT NOT NULL DEFAULT 'Draft'
                        CHECK (status IN ('Draft', 'Slot Creation', 'Booking Open', 'Closed', 'Completed')),
    status_override     INTEGER NOT NULL DEFAULT 0 CHECK (status_override IN (0, 1))
);

-- Admin assigns examiners to an exam
CREATE TABLE IF NOT EXISTS exam_examiners (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id     INTEGER NOT NULL REFERENCES examinations(id),
    examiner_id INTEGER NOT NULL REFERENCES examiner_profiles(id),
    UNIQUE (exam_id, examiner_id)
);

CREATE TABLE IF NOT EXISTS rubrics (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id        INTEGER NOT NULL REFERENCES examinations(id),
    criterion_name TEXT NOT NULL,
    max_marks      REAL NOT NULL,
    weightage      REAL,
    description    TEXT
);

CREATE TABLE IF NOT EXISTS slots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id         INTEGER NOT NULL REFERENCES examinations(id),
    examiner_id     INTEGER NOT NULL REFERENCES examiner_profiles(id),
    slot_date       TEXT NOT NULL,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    capacity        INTEGER NOT NULL CHECK (capacity > 0),
    available_seats INTEGER NOT NULL CHECK (available_seats >= 0),
    location        TEXT,
    status          TEXT NOT NULL DEFAULT 'Available'
                    CHECK (status IN ('Available', 'Full', 'Cancelled', 'Completed'))
);

CREATE TABLE IF NOT EXISTS bookings (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id                INTEGER NOT NULL REFERENCES users(id),
    slot_id                   INTEGER NOT NULL REFERENCES slots(id),
    exam_id                   INTEGER NOT NULL REFERENCES examinations(id),
    booking_date              TEXT NOT NULL DEFAULT (datetime('now')),
    status                    TEXT NOT NULL DEFAULT 'Booked'
                              CHECK (status IN ('Booked', 'Cancelled', 'Completed')),
    cancelled_at              TEXT,
    rescheduled_from_slot_id  INTEGER REFERENCES slots(id)
);
-- One active booking per student per exam (also checked in code, this is a safety net)
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_booking
    ON bookings (student_id, exam_id) WHERE status = 'Booked';

CREATE TABLE IF NOT EXISTS evaluations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id   INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
    examiner_id  INTEGER NOT NULL REFERENCES examiner_profiles(id),
    total_marks  REAL,
    remarks      TEXT,
    status       TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Submitted')),
    is_published INTEGER NOT NULL DEFAULT 0 CHECK (is_published IN (0, 1)),
    evaluated_at TEXT
);

CREATE TABLE IF NOT EXISTS evaluation_scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
    rubric_id     INTEGER NOT NULL REFERENCES rubrics(id),
    marks_awarded REAL NOT NULL
);

-- Audit trail: rows here are never deleted
CREATE TABLE IF NOT EXISTS booking_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id  INTEGER NOT NULL REFERENCES bookings(id),
    action      TEXT NOT NULL CHECK (action IN ('Booked', 'Cancelled', 'Rescheduled')),
    old_slot_id INTEGER REFERENCES slots(id),
    new_slot_id INTEGER REFERENCES slots(id),
    acted_by    INTEGER NOT NULL REFERENCES users(id),
    acted_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
