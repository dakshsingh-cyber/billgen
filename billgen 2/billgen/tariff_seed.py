"""Seed data for UP tariff categories (read from a photo of the official tariff sheet)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from typing import Optional

from models import FixedChargeTier, TariffCategory, TariffSlab, db

NOTE = "Seeded from a photo of the tariff sheet; VERIFY AGAINST OFFICIAL TARIFF ORDER before billing."

Slabs = list[tuple[Optional[str], str]]            # (upper bound or None, rate)
Tiers = list[tuple[str, Optional[str], str]]       # (min load, max load or None, rate)


def flat(rate: str) -> Tiers:
    return [("0", None, rate)]


# code, name, basis, fixed unit, slabs (upper bound, rate), fixed tiers, ED %, FL %
SEED: list[tuple[str, str, str, str, Slabs, Tiers, str, str]] = [
    ("10", "Domestic Light & Fan (individuals/MSB/society, under 50 kW single point)", "KWH", "KW",
     [("150", "5.50"), ("300", "6.00"), ("500", "6.50"), (None, "7.00")], flat("110"), "5", "0"),
    ("13-LIFELINE", "Lifeline consumer (load up to 1 kW, consumption up to 100 units)", "KWH", "KW",
     [("100", "3.00")], flat("50"), "5", "0"),
    ("17", "Metered Rural Schedule (domestic)", "KWH", "KW",
     [("100", "3.35"), ("150", "3.85"), ("300", "5.00"), ("500", "5.50"), (None, "6.00")], flat("90"), "5", "0"),
    ("15", "MES, colony, MSB, township (load above 50 kW, single point)", "KWH", "KW",
     [(None, "7.00")], flat("110"), "5", "0"),
    ("20", "Non-Domestic Light & Fan (shops, hotels, restaurants, banks, cinemas etc.)", "KWH", "KW",
     [("300", "7.50"), ("1000", "8.40"), (None, "8.75")],
     [("0", "2", "330"), ("2", "4", "390"), ("4", None, "450")], "7.5", "0"),
    ("27", "Metered Rural Schedule (non-domestic)", "KWH", "KW", [(None, "5.50")], flat("110"), "7.5", "0"),
    ("33", "Gram Panchayat", "KWH", "KW", [(None, "7.50")], flat("200"), "7.5", "0"),
    ("34", "Nagar Palika and Nagar Panchayat", "KWH", "KW", [(None, "8.00")], flat("250"), "7.5", "0"),
    ("35", "Nagar Nigam", "KWH", "KW", [(None, "8.50")], flat("250"), "7.5", "0"),
    ("41-42", "Public Institutions (Govt. offices, hospitals, guest houses, railway establishments)", "KWH", "KW",
     [("1000", "8.25"), ("2000", "8.50"), (None, "8.75")], flat("300"), "7.5", "0"),
    ("40-48", "Private Institutions (non-govt. hospitals, schools, colleges, trusts)", "KWH", "KW",
     [("1000", "9.00"), (None, "9.30")], [("0", "3", "350"), ("3", None, "400")], "7.5", "0"),
    ("60", "Industrial connection (load less than 100 BHP)", "KWH", "KW",
     [("1000", "7.30"), ("2000", "7.40"), (None, "7.90")], flat("290"), "7.5", "0"),
    ("70", "Public Water Works and pumping stations", "KWH", "KW", [(None, "8.60")], flat("375"), "7.5", "0"),
    ("80", "State tubewells and pump canals", "KWH", "BHP", [(None, "8.50")], flat("330"), "7.5", "0"),
    ("92", "Temporary connection (individual residential construction)", "KWH", "KW",
     [(None, "8.00")], flat("200"), "7.5", "0"),
    ("93", "Temporary connection (others)", "KWH", "KW", [(None, "9.00")], flat("300"), "7.5", "0"),
    ("H11", "Commercial, private institute (load above 75 KW, voltage 11 KV and above)", "KVAH", "KVA",
     [("2500", "8.32"), (None, "8.68")], flat("430"), "7.5", "0"),
    ("H12", "Commercial, private institute (load above 75 KW, voltage 33 KV and above)", "KVAH", "KVA",
     [("2500", "8.12"), (None, "8.48")], flat("400"), "7.5", "0"),
    ("H13", "MSB, colonies, public institute (load above 75 KW, 11 KV)", "KVAH", "KVA",
     [("2500", "7.70"), (None, "7.90")], flat("360"), "7.5", "0"),
    ("H14", "MSB, colonies, public institute (load above 75 KW, 33 KV and above)", "KVAH", "KVA",
     [("2500", "7.50"), (None, "7.70")], flat("360"), "7.5", "0"),
    ("H21", "Industrial (load 100 BHP and above, 11 KV)", "KVAH", "KVA", [(None, "7.10")], flat("300"), "7.5", "0"),
    ("H22", "Industrial (100 BHP and above, 11 KV to 66 KV)", "KVAH", "KVA", [(None, "6.80")], flat("290"), "7.5", "0"),
    ("H23", "Industrial (100 BHP and above, 66 KV to 132 KV)", "KVAH", "KVA", [(None, "6.40")], flat("270"), "7.5", "0"),
    ("H24", "Industrial (100 BHP and above, above 132 KV)", "KVAH", "KVA", [(None, "6.10")], flat("270"), "7.5", "0"),
    ("LMV-2-SAMPLE", "Non-Domestic (matches the sample bill: KVAH basis, Rs 6.75/unit, Rs 405/KVA fixed)", "KVAH", "KVA",
     [(None, "6.75")], flat("405"), "7.5", "0.48"),
]


def seed_tariffs() -> int:
    """Insert the seed categories if the table is empty. Returns the number inserted."""
    if TariffCategory.query.count() > 0:
        return 0
    for code, name, basis, unit, slabs, tiers, ed, fl in SEED:
        cat = TariffCategory(
            code=code, name=name, description="", billing_basis=basis, fixed_charge_unit=unit,
            is_active=True, effective_from=date(2025, 4, 1), notes=NOTE,
            electricity_duty_pct=D(ed), fl_charge_pct=D(fl), tod_applicable=False)
        lower = D("0")
        for upper, rate in slabs:
            cat.slabs.append(TariffSlab(from_unit=lower, to_unit=None if upper is None else D(upper), rate=D(rate)))
            if upper is not None:
                lower = D(upper)
        for lo, hi, rate in tiers:
            cat.fixed_tiers.append(FixedChargeTier(min_load=D(lo), max_load=None if hi is None else D(hi), rate=D(rate)))
        db.session.add(cat)
    db.session.commit()
    return len(SEED)
