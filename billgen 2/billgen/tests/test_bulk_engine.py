import io

import pandas as pd
import pytest

from bulk_engine import build_bills

SLABS = pd.DataFrame({"From": [0, 100, 300], "To": [100, 300, None], "Rate": [5.5, 6.5, 7.5]})
CHARGES = pd.DataFrame({"Name": ["Fixed Charge", "Fuel Surcharge", "GST"],
                        "Type": ["fixed", "percent_of_energy", "percent_of_subtotal"],
                        "Value": [110, 5, 18]})
USAGE = pd.DataFrame({"Customer_ID": ["C001", "C002"], "Name": ["Amit", "Ravi"], "Units": [250, 80]})


def test_sample_bills():
    out = build_bills(USAGE, SLABS, CHARGES)
    xl = pd.ExcelFile(io.BytesIO(out.read()))
    assert xl.sheet_names == ["Summary", "Bill_Details"]
    summary = xl.parse("Summary")
    assert list(summary.columns) == ["Customer_ID", "Name", "Units", "Total_Bill"]
    assert summary.loc[0, "Total_Bill"] == pytest.approx(2019.28)
    assert summary.loc[1, "Total_Bill"] == pytest.approx(674.96)
    details = xl.parse("Bill_Details")
    assert (details["Description"] == "Total").sum() == 2


def test_unknown_charge_type():
    bad = CHARGES.copy()
    bad.loc[0, "Type"] = "weird"
    with pytest.raises(ValueError, match="weird"):
        build_bills(USAGE, SLABS, bad)


def test_negative_units_names_customer():
    bad = USAGE.copy()
    bad.loc[1, "Units"] = -5
    with pytest.raises(ValueError, match="C002"):
        build_bills(bad, SLABS, CHARGES)
