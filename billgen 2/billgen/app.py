"""BillGen - merged Flask app: bulk Excel bills + Uttar Pradesh electricity PDF bills.

CSRF protection is not included in this MVP. Add Flask-WTF (CSRFProtect) before going to production.
"""
from dotenv import load_dotenv

load_dotenv()  # must run before modules that read environment variables are imported

import io
import json
import os
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, Optional

import pandas as pd
from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user

import limits
import payments
from bill_engine import calculate_bill, format_inr, input_to_dict, parse_input, statement_rows
from bulk_engine import build_bills
from models import Bill, FixedChargeTier, Payment, TariffCategory, TariffSlab, User, db
from pdf_builder import build_bill_pdf
from tariff_seed import seed_tariffs

app = Flask(__name__)

secret = os.getenv("SECRET_KEY")
if not secret:
    print("WARNING: SECRET_KEY is not set. Using an insecure development key. Set SECRET_KEY in .env.")
    secret = "dev-only-insecure-key"
app.config["SECRET_KEY"] = secret

db_url = os.getenv("DATABASE_URL", "sqlite:///billgen.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)  # Render/Heroku compatibility
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # two 5 MB files plus form overhead

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "info"

MAX_UPLOAD = 5 * 1024 * 1024
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    return db.session.get(User, int(user_id))


with app.app_context():
    db.create_all()
    seed_tariffs()


# ------------------------------------------------------------------ template helpers
def _ist(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.replace(tzinfo=timezone.utc).astimezone(limits.IST).strftime("%d %b %Y, %I:%M %p")


def _num(v: Any) -> str:
    if v is None or v == "":
        return ""
    return format(Decimal(str(v)).normalize(), "f")


app.add_template_filter(_ist, "ist")
app.add_template_filter(_num, "num")
app.add_template_filter(lambda v, p=2: format_inr(v, p), "inr")


# ------------------------------------------------------------------ login attempt limiter
# In-memory only: it resets on restart and is per-process. Move to Redis for production.
FAIL_LIMIT = 5
WINDOW_SECONDS = 15 * 60
_failed_logins: dict[str, list[float]] = {}
_blocked_until: dict[str, float] = {}


def _is_blocked(email: str) -> bool:
    until = _blocked_until.get(email)
    if until is None:
        return False
    if until > time.time():
        return True
    _blocked_until.pop(email, None)
    _failed_logins.pop(email, None)
    return False


def _register_failure(email: str) -> None:
    now = time.time()
    hits = [t for t in _failed_logins.get(email, []) if now - t < WINDOW_SECONDS] + [now]
    _failed_logins[email] = hits
    if len(hits) >= FAIL_LIMIT:
        _blocked_until[email] = now + WINDOW_SECONDS


def _safe_next(target: Optional[str]) -> Optional[str]:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


# ------------------------------------------------------------------ auth routes
@app.route("/")
def index():
    return redirect(url_for("dashboard" if current_user.is_authenticated else "login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        errors = []
        if not email or "@" not in email or len(email) > 120:
            errors.append("Please enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("signup.html")
        if User.query.filter_by(email=email).first():
            flash("Email already registered", "danger")
            return redirect(url_for("signup"))
        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("Account created. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if _is_blocked(email):
            flash("Too many failed attempts. Please try again in 15 minutes.", "danger")
            return render_template("login.html")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            _failed_logins.pop(email, None)
            login_user(user)
            return redirect(_safe_next(request.form.get("next")) or url_for("dashboard"))
        _register_failure(email)
        flash("Wrong email or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out", "info")
    return redirect(url_for("login"))


# ------------------------------------------------------------------ dashboard
def _render_dashboard(form: Any = None, result: Any = None, rows: Any = None, tab: str = "up", status: int = 200):
    paid = limits.has_active_plan(current_user)
    categories = TariffCategory.query.filter_by(is_active=True).order_by(TariffCategory.code).all()
    html = render_template(
        "dashboard.html", paid=paid, left=limits.remaining_free(current_user),
        paid_until=_ist(current_user.paid_until) if paid else None,
        categories=categories, form=form if form is not None else {}, result=result, rows=rows, tab=tab)
    return html, status


@app.route("/dashboard")
@login_required
def dashboard():
    return _render_dashboard(tab="bulk" if request.args.get("tab") == "bulk" else "up")


# ------------------------------------------------------------------ UP electricity bill (PDF)
def _compute(form: Any):
    """Parse the form and calculate. Raises ValueError with a user-facing message."""
    inp = parse_input(form)
    cat = TariffCategory.query.filter_by(code=inp.category_code, is_active=True).first()
    if cat is None:
        raise ValueError("Please choose a valid tariff category.")
    cat_dict = cat.to_engine_dict()
    return inp, cat_dict, calculate_bill(inp, cat_dict)


def _pdf_name(account_id: str, bill_date: date) -> str:
    return f"bill_{re.sub(r'[^A-Za-z0-9_-]', '_', account_id)}_{bill_date.isoformat()}.pdf"


@app.post("/generate")
@login_required
def generate():
    action = request.form.get("action", "generate")
    # Steps A/B: validate and calculate. Nothing is consumed if either fails.
    try:
        inp, cat_dict, result = _compute(request.form)
    except ValueError as exc:
        flash(str(exc), "danger")
        return _render_dashboard(form=request.form, tab="up")

    if action == "preview":  # preview is always free
        return _render_dashboard(form=request.form, result=result, rows=statement_rows(result), tab="up")

    # Step C: build the PDF first so a failure here costs the user nothing.
    try:
        pdf = build_bill_pdf(result, inp, f"{cat_dict['name']} ({cat_dict['code']})")
    except Exception:
        app.logger.exception("PDF build failed")
        flash("Sorry, the PDF could not be created. Please check your inputs and try again.", "danger")
        return _render_dashboard(form=request.form, tab="up")

    # Step D: only now consume an action. If none is left, discard the PDF.
    if not limits.use_action(current_user):
        flash("You have used your 5 free actions today.", "warning")
        return redirect(url_for("pay"))

    # Step E: save history (the action is already consumed, so never fail the download over this).
    try:
        db.session.add(Bill(
            user_id=current_user.id, consumer_name=inp.consumer_name, account_id=inp.account_id,
            category_code=inp.category_code,
            inputs_json=json.dumps({"inputs": input_to_dict(inp), "category": cat_dict}, default=str),
            result_json=json.dumps(result.to_dict()), total_payable=result.total_payable))
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Could not save bill history")

    return send_file(pdf, as_attachment=True, download_name=_pdf_name(inp.account_id, inp.bill_date),
                     mimetype="application/pdf")


@app.route("/history")
@login_required
def history():
    bills = (Bill.query.filter_by(user_id=current_user.id)
             .order_by(Bill.created_at.desc(), Bill.id.desc()).limit(50).all())
    return render_template("history.html", bills=bills)


@app.route("/history/<int:bill_id>/pdf")
@login_required
def history_pdf(bill_id: int):
    bill = Bill.query.filter_by(id=bill_id, user_id=current_user.id).first_or_404()  # own bills only
    try:
        saved = json.loads(bill.inputs_json)
        inp = parse_input(saved["inputs"])
        cat_dict = saved["category"]
        result = calculate_bill(inp, cat_dict)
        pdf = build_bill_pdf(result, inp, f"{cat_dict['name']} ({cat_dict['code']})")
    except Exception:
        app.logger.exception("History rebuild failed")
        flash("This bill could not be rebuilt.", "danger")
        return redirect(url_for("history"))
    # Re-downloads are free: no action is consumed here.
    return send_file(pdf, as_attachment=True, download_name=_pdf_name(inp.account_id, inp.bill_date),
                     mimetype="application/pdf")


# ------------------------------------------------------------------ bulk Excel bills
@app.post("/generate-bulk")
@login_required
def generate_bulk():
    def bad(message: str):
        flash(message, "danger")
        return redirect(url_for("dashboard", tab="bulk"))

    usage = request.files.get("usage")
    tariff = request.files.get("tariff")
    # Step A: both files present and .xlsx
    if not usage or not tariff or not usage.filename or not tariff.filename:
        return bad("Please upload both the usage sheet and the tariff sheet.")
    if not (usage.filename.lower().endswith(".xlsx") and tariff.filename.lower().endswith(".xlsx")):
        return bad("Both files must be .xlsx Excel files.")
    # Step B: size limit
    blobs = []
    for label, f in (("Usage", usage), ("Tariff", tariff)):
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if size > MAX_UPLOAD:
            return bad(f"{label} file is larger than 5 MB.")
        blobs.append(f.stream.read())

    # Step C: read and check structure
    try:
        usage_df = pd.read_excel(io.BytesIO(blobs[0]), sheet_name=0)
        xl = pd.ExcelFile(io.BytesIO(blobs[1]))
        sheets = xl.sheet_names
        for needed in ("Slabs", "Charges"):
            if needed not in sheets:
                return bad(f'The tariff workbook is missing a sheet named "{needed}".')
        slabs_df = xl.parse("Slabs")
        charges_df = xl.parse("Charges")
    except Exception:
        app.logger.exception("Could not read uploaded Excel files")
        return bad("One of the files could not be read. Make sure it is a valid .xlsx workbook.")

    for df, needed, name in ((usage_df, ["Customer_ID", "Name", "Units"], "Usage sheet"),
                             (slabs_df, ["From", "To", "Rate"], "Slabs sheet"),
                             (charges_df, ["Name", "Type", "Value"], "Charges sheet")):
        have = {str(c).strip() for c in df.columns}
        missing = [c for c in needed if c not in have]
        if missing:
            return bad(f"{name} is missing column(s): {', '.join(missing)}.")

    # Step D: build the Excel BEFORE consuming an action, so bad data never costs anything.
    try:
        out = build_bills(usage_df, slabs_df, charges_df)
    except ValueError as exc:
        return bad(str(exc))
    except Exception:
        app.logger.exception("Bulk build failed")
        return bad("Something went wrong while generating the bills. Please check your files.")

    # Step E: consume the action; discard the output if none are left.
    if not limits.use_action(current_user):
        flash("You have used your 5 free actions today.", "warning")
        return redirect(url_for("pay"))

    return send_file(out, as_attachment=True, download_name="bills.xlsx", mimetype=XLSX_MIME)


# ------------------------------------------------------------------ payments
@app.route("/pay")
@login_required
def pay():
    if limits.has_active_plan(current_user):
        flash("You already have an active plan", "info")
        return redirect(url_for("dashboard"))
    try:
        order = payments.create_order(current_user.id)
        db.session.add(Payment(user_id=current_user.id, order_id=order["id"],
                               amount=payments.PLAN_AMOUNT_PAISE, status="created"))
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Could not create Razorpay order")
        flash("The payment service is unavailable right now. Please try again later.", "danger")
        return redirect(url_for("dashboard"))
    return render_template("pay.html", order=order, key_id=payments.RAZORPAY_KEY_ID,
                           user_email=current_user.email, amount_rupees=payments.PLAN_AMOUNT_PAISE // 100)


def _mark_paid(payment: Payment, payment_id: str) -> None:
    """Mark a payment paid and extend the user's plan (callers check idempotency first)."""
    payment.payment_id = payment_id
    payment.status = "paid"
    limits.activate_plan(payment.user)
    db.session.commit()


@app.post("/payment/verify")
@login_required
def payment_verify():
    # Flow: Razorpay Checkout returns order_id, payment_id and a signature. The signature is an
    # HMAC of "order_id|payment_id" with our secret, so only a genuine payment can produce it.
    order_id = request.form.get("razorpay_order_id", "")
    payment_id = request.form.get("razorpay_payment_id", "")
    signature = request.form.get("razorpay_signature", "")
    if not (order_id and payment_id and signature):
        flash("Payment details were incomplete.", "danger")
        return redirect(url_for("dashboard"))
    payment = Payment.query.filter_by(order_id=order_id, user_id=current_user.id).first()
    if payment is None:
        flash("Payment record not found.", "danger")
        return redirect(url_for("dashboard"))
    if payment.status == "paid":
        flash("Payment already processed", "info")
        return redirect(url_for("dashboard"))
    if not payments.verify_payment_signature(order_id, payment_id, signature):
        payment.status = "failed"
        db.session.commit()
        flash("Payment could not be verified", "danger")
        return redirect(url_for("dashboard"))
    _mark_paid(payment, payment_id)
    flash("Payment successful. Unlimited access for 30 days.", "success")
    return redirect(url_for("dashboard"))


@app.post("/webhook/razorpay")
def razorpay_webhook():
    """Backup for when the browser closes before the redirect. Not login-protected."""
    raw = request.get_data()
    if not payments.verify_webhook_signature(raw, request.headers.get("X-Razorpay-Signature", "")):
        return "invalid signature", 400
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        return "bad payload", 400
    if payload.get("event") in ("payment.captured", "order.paid"):
        entities = (payload.get("payload") or {})
        pay_entity = (entities.get("payment") or {}).get("entity") or {}
        order_entity = (entities.get("order") or {}).get("entity") or {}
        order_id = pay_entity.get("order_id") or order_entity.get("id")
        payment_id = pay_entity.get("id")
        amount = pay_entity.get("amount")
        payment = Payment.query.filter_by(order_id=order_id).first() if order_id else None
        if payment is None or payment.status == "paid":
            return "ok", 200  # nothing to do / already processed (idempotent)
        if amount is not None and amount != payment.amount:
            return "ok", 200  # amount mismatch: ignore
        _mark_paid(payment, payment_id or "")
    return "ok", 200


# ------------------------------------------------------------------ admin: tariffs
def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


@app.route("/admin/tariff")
@admin_required
def admin_tariff():
    cats = TariffCategory.query.order_by(TariffCategory.code).all()
    return render_template("admin_tariff.html", cats=cats)


@app.post("/admin/tariff/<int:cat_id>/toggle")
@admin_required
def admin_tariff_toggle(cat_id: int):
    cat = db.session.get(TariffCategory, cat_id) or abort(404)
    cat.is_active = not cat.is_active
    db.session.commit()
    flash(f"Category {cat.code} is now {'active' if cat.is_active else 'inactive'}.", "success")
    return redirect(url_for("admin_tariff"))


def _dec_field(form: Any, key: str, label: str, hi: str = "100000") -> Decimal:
    raw = form.get(key, "").strip()
    try:
        d = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number.") from None
    if not d.is_finite() or d < 0 or d > Decimal(hi):
        raise ValueError(f"{label} must be between 0 and {hi}.")
    return d


def _apply_category_form(cat: TariffCategory, form: Any) -> None:
    code = form.get("code", "").strip()
    name = form.get("name", "").strip()
    if not code or len(code) > 40:
        raise ValueError("Code is required (max 40 characters).")
    if not name or len(name) > 200:
        raise ValueError("Name is required (max 200 characters).")
    clash = TariffCategory.query.filter(TariffCategory.code == code, TariffCategory.id != (cat.id or 0)).first()
    if clash:
        raise ValueError(f"Another category already uses the code {code}.")
    basis = form.get("billing_basis", "")
    unit = form.get("fixed_charge_unit", "")
    if basis not in ("KWH", "KVAH"):
        raise ValueError("Billing basis must be KWH or KVAH.")
    if unit not in ("KW", "KVA", "BHP", "NONE"):
        raise ValueError("Fixed charge unit must be KW, KVA, BHP or NONE.")
    eff_raw = form.get("effective_from", "").strip()
    try:
        eff = date.fromisoformat(eff_raw) if eff_raw else None
    except ValueError:
        raise ValueError("Effective-from date is invalid.") from None

    # slabs: each row is "up to (blank = no limit)" + rate; the lower bound is the previous upper bound
    slab_rows = []
    lower = Decimal("0")
    to_list, rate_list = form.getlist("slab_to"), form.getlist("slab_rate")
    rows = [(t.strip(), r.strip()) for t, r in zip(to_list, rate_list) if t.strip() or r.strip()]
    if not rows:
        raise ValueError("Add at least one energy slab.")
    for i, (to_raw, rate_raw) in enumerate(rows, start=1):
        if not rate_raw:
            raise ValueError(f"Slab {i}: rate is required.")
        rate = _dec_field({"v": rate_raw}, "v", f"Slab {i} rate")
        if to_raw:
            upper = _dec_field({"v": to_raw}, "v", f"Slab {i} upper limit", "100000000")
            if upper <= lower:
                raise ValueError(f"Slab {i}: upper limit must be greater than {lower}.")
        else:
            upper = None
            if i != len(rows):
                raise ValueError("Only the last slab can have a blank (no limit) upper bound.")
        slab_rows.append(TariffSlab(from_unit=lower, to_unit=upper, rate=rate))
        if upper is not None:
            lower = upper

    tier_rows = []
    for i, (mn, mx, rt) in enumerate(zip(form.getlist("tier_min"), form.getlist("tier_max"),
                                         form.getlist("tier_rate")), start=1):
        mn, mx, rt = mn.strip(), mx.strip(), rt.strip()
        if not (mn or mx or rt):
            continue
        if not rt:
            raise ValueError(f"Fixed-charge tier {i}: rate is required.")
        tmin = _dec_field({"v": mn or "0"}, "v", f"Tier {i} minimum load")
        tmax = _dec_field({"v": mx}, "v", f"Tier {i} maximum load") if mx else None
        if tmax is not None and tmax <= tmin:
            raise ValueError(f"Tier {i}: maximum load must be greater than the minimum.")
        tier_rows.append(FixedChargeTier(min_load=tmin, max_load=tmax,
                                         rate=_dec_field({"v": rt}, "v", f"Tier {i} rate")))
    if unit != "NONE" and not tier_rows:
        raise ValueError("Add at least one fixed-charge tier (or set the fixed charge unit to NONE).")

    cat.code, cat.name, cat.billing_basis, cat.fixed_charge_unit = code, name, basis, unit
    cat.description = form.get("description", "").strip()
    cat.notes = form.get("notes", "").strip()
    cat.effective_from = eff
    cat.is_active = form.get("is_active") == "on"
    cat.tod_applicable = form.get("tod_applicable") == "on"
    cat.electricity_duty_pct = _dec_field(form, "electricity_duty_pct", "Electricity duty %", "100")
    cat.fl_charge_pct = _dec_field(form, "fl_charge_pct", "FL charge %", "100")
    cat.slabs = slab_rows            # delete-orphan removes the old rows
    cat.fixed_tiers = tier_rows


@app.route("/admin/tariff/new", methods=["GET", "POST"])
@app.route("/admin/tariff/<int:cat_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_tariff_edit(cat_id: Optional[int] = None):
    if cat_id is None:
        cat = TariffCategory(code="", name="", description="", billing_basis="KWH", fixed_charge_unit="KW",
                             is_active=True, notes="", electricity_duty_pct=Decimal("7.5"),
                             fl_charge_pct=Decimal("0"), tod_applicable=False)
        is_new = True
    else:
        cat = db.session.get(TariffCategory, cat_id) or abort(404)
        is_new = False
    if request.method == "POST":
        try:
            _apply_category_form(cat, request.form)
            if is_new:
                db.session.add(cat)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(request.url)
        flash("Tariff category saved.", "success")
        return redirect(url_for("admin_tariff"))
    return render_template("admin_tariff_edit.html", cat=cat, is_new=is_new)


# ------------------------------------------------------------------ error pages
def _error(code: int, message: str):
    return render_template("error.html", code=code, message=message), code


@app.errorhandler(403)
def forbidden(_e):
    return _error(403, "You do not have permission to open this page.")


@app.errorhandler(404)
def not_found(_e):
    return _error(404, "We could not find that page.")


@app.errorhandler(413)
def too_large(_e):
    return _error(413, "The upload is too large (maximum 5 MB per file).")


@app.errorhandler(500)
def server_error(_e):
    db.session.rollback()
    return _error(500, "Something went wrong on our side. Please try again.")


if __name__ == "__main__":
    app.run(debug=True)
