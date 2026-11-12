"""Slab-based bulk bill engine (Excel in, Excel out). Pure logic, no Flask imports."""
from __future__ import annotations

import io
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

import pandas as pd
from openpyxl.styles import Font

CHARGE_TYPES = ("fixed", "percent_of_energy", "percent_of_subtotal")
CENT = Decimal("0.01")


def _r2(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def _blank(v: Any) -> bool:
    return v is None or (not isinstance(v, str) and pd.isna(v)) or (isinstance(v, str) and not v.strip())


def _num(v: Any, label: str) -> Decimal:
    if _blank(v):
        raise ValueError(f"{label} is blank; it must be a number.")
    try:
        d = Decimal(str(v).strip())
    except Exception:
        raise ValueError(f"{label} must be numeric (got '{v}').") from None
    if not d.is_finite():
        raise ValueError(f"{label} must be a finite number.")
    return d


def _prepare(df: pd.DataFrame, required: list[str], sheet: str) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{sheet}: missing column(s) {', '.join(missing)}.")
    return df.dropna(how="all").reset_index(drop=True)


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


def _customer_id(v: Any, row_no: int) -> str:
    if _blank(v):
        raise ValueError(f"Usage sheet row {row_no}: Customer_ID is blank.")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def build_bills(usage_df: pd.DataFrame, slabs_df: pd.DataFrame, charges_df: pd.DataFrame) -> io.BytesIO:
    usage = _prepare(usage_df, ["Customer_ID", "Name", "Units"], "Usage sheet")
    slabs_raw = _prepare(slabs_df, ["From", "To", "Rate"], "Slabs sheet")
    charges_raw = _prepare(charges_df, ["Name", "Type", "Value"], "Charges sheet")

    if usage.empty:
        raise ValueError("Usage sheet has no customer rows.")
    if slabs_raw.empty:
        raise ValueError("Slabs sheet has no rows.")

    # ---- slabs: (from, to-or-None, rate), sorted by From ----
    slabs: list[tuple[Decimal, Optional[Decimal], Decimal]] = []
    for i, row in slabs_raw.iterrows():
        n = i + 2
        frm = _num(row["From"], f"Slabs row {n}: From")
        to = None if _blank(row["To"]) else _num(row["To"], f"Slabs row {n}: To")
        rate = _num(row["Rate"], f"Slabs row {n}: Rate")
        if to is not None and to <= frm:
            raise ValueError(f"Slabs row {n}: To ({to}) must be greater than From ({frm}).")
        slabs.append((frm, to, rate))
    slabs.sort(key=lambda s: s[0])

    # ---- charges ----
    charges: list[tuple[str, str, Decimal]] = []
    for i, row in charges_raw.iterrows():
        n = i + 2
        name = "" if _blank(row["Name"]) else str(row["Name"]).strip()
        ctype = "" if _blank(row["Type"]) else str(row["Type"]).strip().lower()
        if ctype not in CHARGE_TYPES:
            raise ValueError(
                f"Charges row {n}: unknown Type '{row['Type']}'. "
                f"Allowed types: {', '.join(CHARGE_TYPES)}.")
        charges.append((name or f"Charge {n}", ctype, _num(row["Value"], f"Charges row {n}: Value")))

    summary_rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []

    for idx, row in usage.iterrows():
        cid = _customer_id(row["Customer_ID"], idx + 2)
        if _blank(row["Units"]):
            raise ValueError(f"Customer {cid}: Units is blank.")
        try:
            units = Decimal(str(row["Units"]).strip())
        except Exception:
            raise ValueError(f"Customer {cid}: Units must be numeric (got '{row['Units']}').") from None
        if not units.is_finite() or units < 0:
            raise ValueError(f"Customer {cid}: Units must be a number >= 0.")
        name = "" if _blank(row["Name"]) else str(row["Name"]).strip()

        lines: list[dict[str, Any]] = []

        # 1. energy charge per slab: consumed = max(0, min(units, upper) - From)
        energy_total = Decimal("0")
        for frm, to, rate in slabs:
            upper = units if to is None else min(units, to)
            consumed = max(Decimal("0"), upper - frm)
            if consumed <= 0:
                continue
            amount = _r2(consumed * rate)
            energy_total += amount
            label = f"Energy {_fmt(frm)} to {_fmt(to)}" if to is not None else f"Energy above {_fmt(frm)}"
            lines.append({"Description": label, "Qty": float(consumed), "Rate": float(rate),
                          "Amount": float(amount)})
        energy_total = _r2(energy_total)

        # 2. subtotal = energy + fixed + percent_of_energy
        subtotal = energy_total
        for cname, ctype, value in charges:
            if ctype == "fixed":
                amount = _r2(value)
                subtotal += amount
                lines.append({"Description": cname, "Qty": None, "Rate": None, "Amount": float(amount)})
            elif ctype == "percent_of_energy":
                amount = _r2(energy_total * value / 100)
                subtotal += amount
                lines.append({"Description": cname, "Qty": None, "Rate": f"{_fmt(value)}%",
                              "Amount": float(amount)})

        # 3. total = subtotal + percent_of_subtotal charges.
        # Each such row is calculated on the SAME subtotal (they are not compounded).
        total = subtotal
        for cname, ctype, value in charges:
            if ctype == "percent_of_subtotal":
                amount = _r2(subtotal * value / 100)
                total += amount
                lines.append({"Description": cname, "Qty": None, "Rate": f"{_fmt(value)}%",
                              "Amount": float(amount)})
        total = _r2(total)

        for ln in lines:
            detail_rows.append({"Customer_ID": cid, **ln})
        detail_rows.append({"Customer_ID": cid, "Description": "Total", "Qty": None, "Rate": None,
                            "Amount": float(total)})
        summary_rows.append({"Customer_ID": cid, "Name": name, "Units": float(units),
                             "Total_Bill": float(total)})

    summary = pd.DataFrame(summary_rows, columns=["Customer_ID", "Name", "Units", "Total_Bill"])
    details = pd.DataFrame(detail_rows, columns=["Customer_ID", "Description", "Qty", "Rate", "Amount"])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        details.to_excel(writer, sheet_name="Bill_Details", index=False)
        for sheet_name, frame in (("Summary", summary), ("Bill_Details", details)):
            ws = writer.sheets[sheet_name]
            for cell in ws[1]:
                cell.font = Font(bold=True)
            for col_idx, col in enumerate(frame.columns, start=1):
                longest = max([len(str(col))] + [len(str(v)) for v in frame[col].tolist() if not _blank(v)])
                ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(max(longest + 3, 12), 45)
    buf.seek(0)
    return buf
