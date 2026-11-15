import re
from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user

from . import db
from .models import User

bp = Blueprint("auth", __name__)

SIGNUP_ROLES = {"inventory_clerk", "sales_personnel"}  # admin/instructor are created by an admin
HOME = {
    "admin": "main.dashboard",
    "instructor": "main.dashboard",
    "inventory_clerk": "main.inventory",
    "sales_personnel": "main.sales",
}


def home_for(user):
    return url_for(HOME[user.role])


@bp.route("/", methods=["GET", "POST"])
@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(request.form.get("password", "")):
            flash("Invalid username or password.")
        elif user.status == "pending":
            flash("Your account is pending admin approval.")
        elif user.status == "locked":
            flash("Your account is locked. Contact an admin.")
        else:
            login_user(user)
            next_url = request.args.get("next", "")
            parsed_next = urlparse(next_url)
            if (
                next_url.startswith("/")
                and not next_url.startswith("//")
                and "\\" not in next_url
                and not parsed_next.netloc
                and not parsed_next.scheme
            ):
                return redirect(next_url)
            return redirect(home_for(user))
    return render_template("login.html")


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        f = request.form
        username, pw = f.get("username", "").strip(), f.get("password", "")
        full_name = f.get("full_name", "").strip()
        email = f.get("email", "").strip()
        error = None
        if not full_name or len(full_name) > 120 or not username or len(username) > 64 or not email or len(email) > 120:
            error = "Enter a name, username, and email within the allowed lengths."
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            error = "Enter a valid email address."
        elif pw != f.get("confirm_password", ""):
            error = "Passwords do not match."
        elif (
            len(pw) < 8
            or not re.search(r"[A-Za-z]", pw)
            or not re.search(r"\d", pw)
            or not re.search(r"[^A-Za-z0-9\s]", pw)
        ):
            error = "Password needs 8+ characters with a letter, a number, and a special character."
        elif f.get("role") not in SIGNUP_ROLES:
            error = "Please select a valid role."
        elif User.query.filter_by(username=username).first():
            error = "Username is already taken."
        if error:
            flash(error)
        else:
            u = User(full_name=full_name, email=email,
                     username=username, role=f["role"], status="pending")
            u.set_password(pw)
            db.session.add(u)
            db.session.commit()
            flash("Account created. Wait for admin approval, then log in.")
            return redirect(url_for("auth.login"))
    return render_template("signup.html")


@bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
