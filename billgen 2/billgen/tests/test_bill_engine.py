from datetime import date
from decimal import Decimal as D

import pytest

from bill_engine import BillInput, calculate_bill, format_inr, parse_input, input_to_dict

SAMPLE_CAT = {
    "code": "LMV-2", "name": "Non-Domestic", "billing_basis": "KVAH", "fixed_charge_unit": "KVA",
    "electricity_duty_pct": D("7.5"), "fl_charge_pct": D("0"),
    "slabs": [{"from_unit": D(0), "to_unit": None, "rate": D("6.75")}],
    "fixed_tiers": [{"min_load": D(0), "max_load": None, "rate": D("405")}],
}


def make(**kw):
    base = dict(consumer_name="T", account_id="1", category_code="X", connection_load=D("2"), load_unit="KW",
                bill_date=date(2025, 5, 1), previous_read_date=date(2025, 4, 1), current_read_date=date(2025, 5, 1))
    base.update(kw)
    return BillInput(**base)


def test_acceptance_sample_bill():
    inp = make(connection_load=D("11.11"), load_unit="KVA", billing_months=19,
               previous_kwh=D(2122), current_kwh=D(21780), previous_kvah=D(2210), current_kvah=D(23066),
               solar_export_units=D(9580), energy_rate_override=D("6.75"), fixed_rate_override=D(405),
               fl_charge_pct_override=D("0.48"), electricity_duty_pct_override=D("7.5"), paid_amount=D(178060))
    r = calculate_bill(inp, SAMPLE_CAT)
    assert r.kwh_diff == 19658 and r.kvah_diff == 20856 and r.billed_units == 11276
    assert r.ec == 76113 and r.fc == 85491 and r.subtotal_ec_fc == 161604
    assert r.fl == 776 and r.total_with_fl == 162380 and r.ed == 12178
    assert r.total == 174558 and r.total_payable == 174558
    assert r.balance == -3502 and r.is_credit


def test_domestic_slabs_320_units():
    cat = {"code": "10", "name": "Domestic", "billing_basis": "KWH", "fixed_charge_unit": "KW",
           "electricity_duty_pct": D(5), "fl_charge_pct": D(0),
           "slabs": [{"from_unit": D(0), "to_unit": D(150), "rate": D("5.5")},
                     {"from_unit": D(150), "to_unit": D(300), "rate": D("6.0")},
                     {"from_unit": D(300), "to_unit": D(500), "rate": D("6.5")},
                     {"from_unit": D(500), "to_unit": None, "rate": D("7.0")}],
           "fixed_tiers": [{"min_load": D(0), "max_load": None, "rate": D(110)}]}
    r = calculate_bill(make(connection_load=D(2), billing_months=1, previous_kwh=D(0), current_kwh=D(320)), cat)
    # 150*5.5 + 150*6.0 + 20*6.5 = 825 + 900 + 130
    assert r.ec == 1855
    assert r.fc == 220
    assert r.subtotal_ec_fc == 2075
    assert r.ed == 104          # 103.75 -> 104
    assert r.total == 2179


def test_nondomestic_tiered_fixed_charge_5kw_uses_450():
    cat = {"code": "20", "name": "Non-Domestic", "billing_basis": "KWH", "fixed_charge_unit": "KW",
           "electricity_duty_pct": D("7.5"), "fl_charge_pct": D(0),
           "slabs": [{"from_unit": D(0), "to_unit": None, "rate": D("7.5")}],
           "fixed_tiers": [{"min_load": D(0), "max_load": D(2), "rate": D(330)},
                           {"min_load": D(2), "max_load": D(4), "rate": D(390)},
                           {"min_load": D(4), "max_load": None, "rate": D(450)}]}
    r = calculate_bill(make(connection_load=D(5), billing_months=1, current_kwh=D(100)), cat)
    assert r.fc_rate == 450 and r.fc == 2250


def test_negative_reading_rejected():
    with pytest.raises(ValueError, match="KWH"):
        calculate_bill(make(previous_kwh=D(500), current_kwh=D(100)), SAMPLE_CAT)


def test_parse_roundtrip_and_validation():
    inp = make(previous_kwh=D(1), current_kwh=D(5))
    again = parse_input(input_to_dict(inp))
    assert again.current_kwh == 5 and again.bill_date == inp.bill_date
    d = input_to_dict(inp)
    d["connection_load"] = "abc"
    with pytest.raises(ValueError):
        parse_input(d)


def test_format_inr():
    assert format_inr(174558) == "1,74,558.00"
    assert format_inr(-3502, 0) == "-3,502"
    assert format_inr(999) == "999.00"
