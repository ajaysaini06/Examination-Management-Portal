# app.py - creates the Flask app, registers blueprints and runs the server
from flask import Flask, render_template

import config
from flask import session
from database import close_db, get_db, init_db
from helpers import refresh_all_exams
from routes.admin import admin_bp
from routes.auth import auth_bp
from routes.examiner import examiner_bp
from routes.student import student_bp


def create_app():
    app = Flask(__name__)
    app.config.from_object(config)

    app.teardown_appcontext(close_db)
    init_db(app)   # creates tables + seeds admin on first run

    @app.before_request
    def keep_exam_status_fresh():
        # Status automation: statuses follow today's date unless admin overrode them
        if "user_id" in session:
            refresh_all_exams(get_db())

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(examiner_bp, url_prefix="/examiner")
    app.register_blueprint(student_bp, url_prefix="/student")

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403,
                               message="You are not allowed to open this page."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404,
                               message="Page not found."), 404

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
