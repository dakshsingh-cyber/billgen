"""Uttar Pradesh electricity bill engine. Pure logic: no Flask, no database.

All money maths uses Decimal. CALCULATION ORDER (matches the real sample bill):
  1. meter differences (kWh, kVAh)           2. gross units by billing basis
  3. billed units = gross - solar export     4. energy charge (EC), slabs applied PER MONTH
  5. fixed charge (FC) = months x load x rate
  6. subtotal (EC+FC) = EC + FC + penalty demand + MCG
  7. FL charge = subtotal x FL%              8. total with FL
  9. electricity duty (ED) = total with FL x ED%
 10. total = total with FL + ED              11. payable = total + arrear + LPSC - security
 12. balance = payable - paid (negative = credit / advance)

ROUNDING: EC, FC, FL and ED are each rounded to a whole rupee with ROUND_HALF_DOWN
(12178.5 -> 12178, 775.7 -> 776). Slab lines are rounded to 2 decimals internally and
the EC total is rounded to a whole rupee at the end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, Optional

ZERO = Decimal("0")
PF_ASSUMED = Decimal("0.9")  # power factor used for KW <-> KVA conversion


# ---------------------------------------------------------------- formatting helpers
def q0(v: Decimal) -> Decimal:
    return v.quantize(Decimal("1"), rounding=ROUND_HALF_DOWN)


def q2(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def format_inr(value: Any, places: int = 2) -> str:
    """Indian digit grouping, e.g. 174558 -> '1,74,558.00'."""
    d = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    int_part, _, frac = f"{abs(d):f}".partition(".")
    if len(int_part) > 3:
        head, tail = int_part[:-3], int_part[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        int_part = ",".join(groups + [tail])
    return sign + int_part + (f".{frac}" if places else "")


def fmt_qty(v: Decimal) -> str:
    return format_inr(v, 0 if v == v.to_integral_value() else 2)


def _pct(d: Decimal) -> str:
    return format(d.normalize(), "f")


# ---------------------------------------------------------------- data classes
@dataclass
class BillInput:
    consumer_name: str
    account_id: str
    category_code: str
    connection_load: Decimal
    load_unit: str
    bill_date: date
    previous_read_date: date
    current_read_date: date
    address: str = ""
    mobile: str = ""
    previous_kwh: Decimal = ZERO
    current_kwh: Decimal = ZERO
    previous_kvah: Decimal = ZERO
    current_kvah: Decimal = ZERO
    solar_export_units: Decimal = ZERO
    billing_months: Optional[int] = None
    energy_rate_override: Optional[Decimal] = None
    fixed_rate_override: Optional[Decimal] = None
    fl_charge_pct_override: Optional[Decimal] = None
    electricity_duty_pct_override: Optional[Decimal] = None
    penalty_demand: Decimal = ZERO
    mcg: Decimal = ZERO
    arrear: Decimal = ZERO
    lpsc: Decimal = ZERO
    security_adjustment: Decimal = ZERO
    paid_amount: Decimal = ZERO


@dataclass
class LineItem:
    description: str
    qty: Decimal
    rate: Decimal
    amount: Decimal
    kind: str  # "ec" or "fc"


@dataclass
class BillResult:
    kwh_diff: Decimal
    kvah_diff: Decimal
    gross_units: Decimal
    solar_export_units: Decimal
    billed_units: Decimal
    billing_months: int
    billing_basis: str
    lines: list[LineItem]
    ec: Decimal
    fc: Decimal
    fc_load: Decimal
    fc_load_unit: str
    fc_rate: Decimal
    penalty_demand: Decimal
    mcg: Decimal
    subtotal_ec_fc: Decimal
    fl_pct: Decimal
    fl: Decimal
    total_with_fl: Decimal
    ed_pct: Decimal
    ed: Decimal
    total: Decimal
    arrear: Decimal
    lpsc: Decimal
    security_adjustment: Decimal
    total_payable: Decimal
    paid_amount: Decimal
    balance: Decimal
    is_credit: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass
class Row:
    description: str
    qty: str = ""
    rate: str = ""
    amount: str = ""
    style: str = "normal"  # normal / bold / highlight


def jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, date):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return {k: jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


# ---------------------------------------------------------------- input parsing / validation
def _text(data: Mapping[str, Any], key: str) -> str:
    v = data.get(key)
    return "" if v is None else str(v).strip()


def _dec(data: Mapping[str, Any], key: str, label: str, *, default: Optional[Decimal] = ZERO,
         lo: Decimal = ZERO, hi: Decimal = Decimal("1000000000")) -> Optional[Decimal]:
    """Parse a Decimal. Blank -> default (None means 'required' unless default is None via _opt)."""
    raw = _text(data, key).replace(",", "")
    if raw == "":
        return default
    try:
        d = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number.") from None
    if not d.is_finite():
        raise ValueError(f"{label} must be a valid number.")
    if d < lo or d > hi:
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return d


def _req(data: Mapping[str, Any], key: str, label: str, **kw: Any) -> Decimal:
    if _text(data, key) == "":
        raise ValueError(f"{label} is required.")
    return _dec(data, key, label, **kw)  # type: ignore[return-value]


def _date(data: Mapping[str, Any], key: str, label: str) -> date:
    raw = _text(data, key)
    if not raw:
        raise ValueError(f"{label} is required.")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{label} is not a valid date.") from None


def _limited(data: Mapping[str, Any], key: str, label: str, max_len: int, required: bool) -> str:
    v = _text(data, key)
    if required and not v:
        raise ValueError(f"{label} is required.")
    if len(v) > max_len:
        raise ValueError(f"{label} must be at most {max_len} characters.")
    return v


def months_between(start: date, end: date) -> int:
    """Whole months between two read dates (minimum 1)."""
    months = (end.year - start.year) * 12 + (end.month - start.month) - (1 if end.day < start.day else 0)
    return max(1, months)


def parse_input(data: Mapping[str, Any]) -> BillInput:
    """Validate raw form/JSON values (all strings) and build a BillInput. Raises ValueError."""
    units_hi = Decimal("100000000")
    rate_hi = Decimal("100000")
    name = _limited(data, "consumer_name", "Consumer name", 120, True)
    account = _limited(data, "account_id", "Account ID", 60, True)
    address = _limited(data, "address", "Address", 300, False)
    mobile = _limited(data, "mobile", "Mobile", 20, False)
    code = _limited(data, "category_code", "Category", 40, True)

    load = _req(data, "connection_load", "Connection load", lo=Decimal("0.01"), hi=Decimal("100000"))
    load_unit = _text(data, "load_unit").upper() or "KW"
    if load_unit not in ("KW", "KVA"):
        raise ValueError("Load unit must be KW or KVA.")

    bill_date = _date(data, "bill_date", "Bill date")
    prev_date = _date(data, "previous_read_date", "Previous reading date")
    cur_date = _date(data, "current_read_date", "Current reading date")
    if cur_date < prev_date:
        raise ValueError("Current reading date cannot be before the previous reading date.")

    months: Optional[int] = None
    raw_months = _text(data, "billing_months")
    if raw_months:
        try:
            months = int(raw_months)
        except ValueError:
            raise ValueError("Billing months must be a whole number.") from None
        if not 1 <= months <= 120:
            raise ValueError("Billing months must be between 1 and 120.")

    def opt(key: str, label: str, hi: Decimal) -> Optional[Decimal]:
        return _dec(data, key, label, default=None, hi=hi)

    return BillInput(
        consumer_name=name, account_id=account, category_code=code,
        connection_load=load, load_unit=load_unit,
        bill_date=bill_date, previous_read_date=prev_date, current_read_date=cur_date,
        address=address, mobile=mobile,
        previous_kwh=_dec(data, "previous_kwh", "Previous KWH", hi=units_hi),
        current_kwh=_dec(data, "current_kwh", "Current KWH", hi=units_hi),
        previous_kvah=_dec(data, "previous_kvah", "Previous KVAH", hi=units_hi),
        current_kvah=_dec(data, "current_kvah", "Current KVAH", hi=units_hi),
        solar_export_units=_dec(data, "solar_export_units", "Solar export units", hi=units_hi),
        billing_months=months,
        energy_rate_override=opt("energy_rate_override", "Energy rate", rate_hi),
        fixed_rate_override=opt("fixed_rate_override", "Fixed charge rate", rate_hi),
        fl_charge_pct_override=opt("fl_charge_pct_override", "FL charge %", Decimal("100")),
        electricity_duty_pct_override=opt("electricity_duty_pct_override", "Electricity duty %", Decimal("100")),
        penalty_demand=_dec(data, "penalty_demand", "Penalty demand"),
        mcg=_dec(data, "mcg", "MCG"),
        arrear=_dec(data, "arrear", "Arrear"),
        lpsc=_dec(data, "lpsc", "LPSC"),
        security_adjustment=_dec(data, "security_adjustment", "Security adjustment"),
        paid_amount=_dec(data, "paid_amount", "Paid amount"),
    )


def input_to_dict(inp: BillInput) -> dict[str, str]:
    """Serialise to strings that parse_input() can read back (blank = not set)."""
    out: dict[str, str] = {}
    for k in inp.__dataclass_fields__:
        v = getattr(inp, k)
        if v is None:
            out[k] = ""
        elif isinstance(v, Decimal):
            out[k] = format(v, "f")
        else:
            out[k] = str(v)
    return out


# ---------------------------------------------------------------- calculation
def _d(v: Any) -> Decimal:
    return Decimal(str(v if v is not None else 0))


def calculate_bill(inp: BillInput, category: Mapping[str, Any]) -> BillResult:
    if not category:
        raise ValueError("Tariff category not found.")
    basis = category["billing_basis"]
    fc_unit = category["fixed_charge_unit"]
    notes: list[str] = []

    # 1-3. meter differences, gross and billed units
    kwh_diff = inp.current_kwh - inp.previous_kwh
    kvah_diff = inp.current_kvah - inp.previous_kvah
    if kwh_diff < 0:
        raise ValueError(f"Current KWH reading ({inp.current_kwh}) cannot be less than the previous KWH reading ({inp.previous_kwh}).")
    if kvah_diff < 0:
        raise ValueError(f"Current KVAH reading ({inp.current_kvah}) cannot be less than the previous KVAH reading ({inp.previous_kvah}).")
    gross = kvah_diff if basis == "KVAH" else kwh_diff
    if basis == "KVAH" and kvah_diff == 0 and kwh_diff > 0:
        notes.append("This category bills on KVAH but the KVAH readings show no consumption.")
    billed = max(ZERO, gross - inp.solar_export_units)
    if inp.solar_export_units > gross:
        notes.append("Solar export exceeds consumption; the surplus is not carried forward in this bill.")

    months = inp.billing_months or months_between(inp.previous_read_date, inp.current_read_date)
    if months < 1:
        raise ValueError("Billing months must be at least 1.")

    # 4. energy charge
    lines: list[LineItem] = []
    if inp.energy_rate_override is not None:
        rate = inp.energy_rate_override
        amount = q2(billed * rate)
        lines.append(LineItem("Energy charge (flat rate)", billed, rate, amount, "ec"))
        ec = q0(amount)
    else:
        slabs = sorted(category.get("slabs") or [], key=lambda s: _d(s["from_unit"]))
        if not slabs:
            raise ValueError("This category has no energy slabs. Add slabs in the admin page or enter an energy rate.")
        top = slabs[-1].get("to_unit")
        units_pm = billed / months
        if top is not None and units_pm > _d(top):
            raise ValueError(
                f"Units per month ({q2(units_pm)}) exceed the highest slab ({_d(top)}) for this category. "
                "Update the tariff or enter an energy rate.")
        total_amount = ZERO
        for s in slabs:
            lower = _d(s["from_unit"])
            upper = units_pm if s.get("to_unit") is None else min(units_pm, _d(s["to_unit"]))
            band = max(ZERO, upper - lower)
            if band <= 0:
                continue
            rate = _d(s["rate"])
            qty = q2(band * months)
            amount = q2(band * rate * months)
            total_amount += amount
            hi = "above" if s.get("to_unit") is None else f"to {_pct(_d(s['to_unit']))}"
            lines.append(LineItem(f"Energy charge: slab {_pct(lower)} {hi} units/month", qty, rate, amount, "ec"))
        ec = q0(total_amount)
        if not lines:
            lines.append(LineItem("Energy charge", ZERO, ZERO, ZERO, "ec"))

    # 5. fixed charge
    fc_load = ZERO
    fc_rate = ZERO
    fc = ZERO
    if fc_unit != "NONE":
        load = inp.connection_load
        if fc_unit == "KVA" and inp.load_unit == "KW":
            load = q2(load / PF_ASSUMED)
            notes.append(f"Load entered in KW; converted to {load} KVA assuming power factor 0.9.")
        elif fc_unit == "KW" and inp.load_unit == "KVA":
            load = q2(load * PF_ASSUMED)
            notes.append(f"Load entered in KVA; converted to {load} KW assuming power factor 0.9.")
        elif fc_unit == "BHP":
            notes.append("This category is charged per BHP; the connection load is taken as BHP.")
        fc_load = load
        if inp.fixed_rate_override is not None:
            fc_rate = inp.fixed_rate_override
        else:
            tiers = sorted(category.get("fixed_tiers") or [], key=lambda t: _d(t["min_load"]))
            chosen = None
            for t in tiers:
                if _d(t["min_load"]) <= load and (t.get("max_load") is None or load < _d(t["max_load"])):
                    chosen = t
                    break
            if chosen is None:
                raise ValueError(f"No fixed-charge tier covers a load of {load} {fc_unit}. Enter a fixed charge rate.")
            fc_rate = _d(chosen["rate"])
        fc = q0(months * load * fc_rate)
        lines.append(LineItem(
            f"Fixed charge ({months} months x {_pct(load)} {fc_unit} x Rs {_pct(fc_rate)})",
            q2(months * load), fc_rate, fc, "fc"))

    # 6-12. totals
    subtotal = ec + fc + inp.penalty_demand + inp.mcg
    fl_pct = inp.fl_charge_pct_override if inp.fl_charge_pct_override is not None else _d(category.get("fl_charge_pct"))
    ed_pct = (inp.electricity_duty_pct_override if inp.electricity_duty_pct_override is not None
              else _d(category.get("electricity_duty_pct")))
    fl = q0(subtotal * fl_pct / 100)
    total_with_fl = subtotal + fl
    ed = q0(total_with_fl * ed_pct / 100)
    total = total_with_fl + ed
    payable = total + inp.arrear + inp.lpsc - inp.security_adjustment
    balance = payable - inp.paid_amount

    return BillResult(
        kwh_diff=kwh_diff, kvah_diff=kvah_diff, gross_units=gross,
        solar_export_units=inp.solar_export_units, billed_units=billed,
        billing_months=months, billing_basis=basis, lines=lines, ec=ec, fc=fc,
        fc_load=fc_load, fc_load_unit=fc_unit, fc_rate=fc_rate,
        penalty_demand=inp.penalty_demand, mcg=inp.mcg, subtotal_ec_fc=subtotal,
        fl_pct=fl_pct, fl=fl, total_with_fl=total_with_fl, ed_pct=ed_pct, ed=ed,
        total=total, arrear=inp.arrear, lpsc=inp.lpsc, security_adjustment=inp.security_adjustment,
        total_payable=payable, paid_amount=inp.paid_amount, balance=balance,
        is_credit=balance < 0, notes=notes,
    )


def statement_rows(r: BillResult) -> list[Row]:
    """Charge statement rows shared by the HTML preview and the PDF."""
    rows = [Row(ln.description, fmt_qty(ln.qty), format_inr(ln.rate), format_inr(ln.amount)) for ln in r.lines]
    rows += [
        Row("Penalty demand", amount=format_inr(r.penalty_demand)),
        Row("MCG (minimum monthly guarantee)", amount=format_inr(r.mcg)),
        Row("Total (EC+FC)", amount=format_inr(r.subtotal_ec_fc), style="bold"),
        Row(f"FL charge ({_pct(r.fl_pct)}%)", amount=format_inr(r.fl)),
        Row("Total with FL", amount=format_inr(r.total_with_fl)),
        Row(f"Electricity duty ({_pct(r.ed_pct)}%)", amount=format_inr(r.ed)),
        Row("Total", amount=format_inr(r.total), style="bold"),
        Row("Arrear", amount=format_inr(r.arrear)),
        Row("LPSC (late payment surcharge)", amount=format_inr(r.lpsc)),
        Row("Less: security adjustment", amount=format_inr(r.security_adjustment)),
        Row("TOTAL PAYABLE", amount=format_inr(r.total_payable), style="highlight"),
        Row("Paid amount", amount=format_inr(r.paid_amount)),
        Row("Credit (advance)" if r.is_credit else "Balance payable",
            amount=format_inr(abs(r.balance)), style="bold"),
    ]
    return rows
