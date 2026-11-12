"""Razorpay helpers (no route code)."""
from __future__ import annotations

import os
import time
from typing import Any

import razorpay
from razorpay.errors import SignatureVerificationError

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

PLAN_AMOUNT_PAISE = 49900  # Rs 499
PLAN_CURRENCY = "INR"

client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


def create_order(user_id: int) -> dict[str, Any]:
    receipt = f"u{user_id}-{int(time.time())}"  # well under Razorpay's 40-char limit
    return client.order.create({
        "amount": PLAN_AMOUNT_PAISE,
        "currency": PLAN_CURRENCY,
        "receipt": receipt,
        "payment_capture": 1,
    })


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })
        return True
    except SignatureVerificationError:
        return False


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    if not RAZORPAY_WEBHOOK_SECRET or not signature:
        return False  # never accept webhooks when no secret is configured
    try:
        client.utility.verify_webhook_signature(
            raw_body.decode("utf-8"), signature, RAZORPAY_WEBHOOK_SECRET)
        return True
    except (SignatureVerificationError, UnicodeDecodeError):
        return False
