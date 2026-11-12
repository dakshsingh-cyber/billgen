"""Database models for BillGen."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def utcnow() -> datetime:
    """Naive UTC 'now' (all DateTime columns store naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(UserMixin, db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    usage_day = db.Column(db.Date, nullable=True)       # IST calendar day the counter belongs to
    usage_count = db.Column(db.Integer, default=0)      # actions used on usage_day
    paid_until = db.Column(db.DateTime, nullable=True)  # UTC subscription expiry
    created_at = db.Column(db.DateTime, default=utcnow)

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    @property
    def is_admin(self) -> bool:
        admins = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
        return (self.email or "").lower() in admins


class Payment(db.Model):
    __tablename__ = "payment"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    order_id = db.Column(db.String(100), unique=True, nullable=False)
    payment_id = db.Column(db.String(100), nullable=True)
    amount = db.Column(db.Integer)                       # paise
    status = db.Column(db.String(20), default="created")  # created / paid / failed
    created_at = db.Column(db.DateTime, default=utcnow)

    user = db.relationship("User")


class TariffCategory(db.Model):
    __tablename__ = "tariff_category"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    billing_basis = db.Column(db.String(4), nullable=False, default="KWH")        # KWH / KVAH
    fixed_charge_unit = db.Column(db.String(4), nullable=False, default="KW")     # KW / KVA / BHP / NONE
    is_active = db.Column(db.Boolean, default=True)
    effective_from = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, default="")
    electricity_duty_pct = db.Column(db.Numeric(8, 4), default=0)
    fl_charge_pct = db.Column(db.Numeric(8, 4), default=0)
    tod_applicable = db.Column(db.Boolean, default=False)

    slabs = db.relationship(
        "TariffSlab", cascade="all, delete-orphan", order_by="TariffSlab.from_unit",
        backref="category", lazy="selectin",
    )
    fixed_tiers = db.relationship(
        "FixedChargeTier", cascade="all, delete-orphan", order_by="FixedChargeTier.min_load",
        backref="category", lazy="selectin",
    )

    def to_engine_dict(self) -> dict[str, Any]:
        """Plain dict for bill_engine (the engine never touches the database)."""
        def d(v: Any) -> Decimal:
            return Decimal(str(v if v is not None else 0))

        def dn(v: Any) -> Decimal | None:
            return None if v is None else Decimal(str(v))

        return {
            "code": self.code,
            "name": self.name,
            "billing_basis": self.billing_basis,
            "fixed_charge_unit": self.fixed_charge_unit,
            "electricity_duty_pct": d(self.electricity_duty_pct),
            "fl_charge_pct": d(self.fl_charge_pct),
            "slabs": [{"from_unit": d(s.from_unit), "to_unit": dn(s.to_unit), "rate": d(s.rate)}
                      for s in self.slabs],
            "fixed_tiers": [{"min_load": d(t.min_load), "max_load": dn(t.max_load), "rate": d(t.rate)}
                            for t in self.fixed_tiers],
        }


class TariffSlab(db.Model):
    __tablename__ = "tariff_slab"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("tariff_category.id"), nullable=False)
    from_unit = db.Column(db.Numeric(14, 4), nullable=False)
    to_unit = db.Column(db.Numeric(14, 4), nullable=True)   # NULL = no upper limit
    rate = db.Column(db.Numeric(10, 4), nullable=False)


class FixedChargeTier(db.Model):
    __tablename__ = "fixed_charge_tier"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("tariff_category.id"), nullable=False)
    min_load = db.Column(db.Numeric(14, 4), nullable=False)
    max_load = db.Column(db.Numeric(14, 4), nullable=True)  # NULL = no limit
    rate = db.Column(db.Numeric(10, 4), nullable=False)     # per load unit per month


class Bill(db.Model):
    __tablename__ = "bill"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    consumer_name = db.Column(db.String(120))
    account_id = db.Column(db.String(60))
    category_code = db.Column(db.String(40))
    inputs_json = db.Column(db.Text)
    result_json = db.Column(db.Text)
    total_payable = db.Column(db.Numeric(14, 2))
