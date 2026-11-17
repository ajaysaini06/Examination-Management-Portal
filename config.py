# config.py - central settings for the app
import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

SECRET_KEY = "46519bc26a679a99d252807a1bf374747295ef7cf21cb6d422e2691133bf4576"
DATABASE = os.path.join(BASE_DIR, "instance", "emp.db")

# Admin account created automatically on first run (no registration page for admin)
ADMIN_NAME = "System Admin"
ADMIN_EMAIL = "admin@emp.com"
ADMIN_PASSWORD = "admin123"
