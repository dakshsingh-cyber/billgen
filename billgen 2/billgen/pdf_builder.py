"""PDF bill builder (reportlab only, no Flask)."""
from __future__ import annotations

import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from bill_engine import BillInput, BillResult, fmt_qty, statement_rows

GREY = colors.HexColor("#e9ecef")
HILITE = colors.HexColor("#cfe2ff")
LINE = colors.HexColor("#6c757d")


def build_bill_pdf(result: BillResult, inp: BillInput, category_name: str) -> io.BytesIO:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            title="Electricity Bill", author="BillGen UP")
    width = A4[0] - 30 * mm
    base = getSampleStyleSheet()["Normal"]
    cell = ParagraphStyle("cell", parent=base, fontName="Helvetica", fontSize=9, leading=11)
    cell_b = ParagraphStyle("cell_b", parent=cell, fontName="Helvetica-Bold")
    title = ParagraphStyle("title", parent=base, fontName="Helvetica-Bold", fontSize=18, leading=22)
    sub = ParagraphStyle("sub", parent=base, fontName="Helvetica", fontSize=10, textColor=LINE)
    small_r = ParagraphStyle("small_r", parent=sub, alignment=2, fontSize=9)
    section = ParagraphStyle("section", parent=base, fontName="Helvetica-Bold", fontSize=11, spaceBefore=8, spaceAfter=3)
    foot = ParagraphStyle("foot", parent=sub, fontSize=8, spaceBefore=8)

    def P(text: object, style: ParagraphStyle = cell) -> Paragraph:
        return Paragraph(escape(str(text)), style)

    story: list = []

    # 1. header
    head = Table([[P("ELECTRICITY BILL", title), P(f"Bill date: {inp.bill_date.strftime('%d %b %Y')}", small_r)],
                  [P("Uttar Pradesh DISCOM (generated bill)", sub), ""]],
                 colWidths=[width * 0.65, width * 0.35])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 4 * mm)]

    # 2. consumer box
    info = [
        ("Consumer name", inp.consumer_name),
        ("Account ID", inp.account_id),
        ("Address", inp.address or "-"),
        ("Mobile", inp.mobile or "-"),
        ("Category", category_name),
        ("Connection load", f"{fmt_qty(inp.connection_load)} {inp.load_unit}"),
        ("Bill date", inp.bill_date.strftime("%d %b %Y")),
        ("Reading period", f"{inp.previous_read_date.strftime('%d %b %Y')} to {inp.current_read_date.strftime('%d %b %Y')}"),
        ("Billing months", str(result.billing_months)),
    ]
    box = Table([[P(k, cell_b), P(v)] for k, v in info], colWidths=[width * 0.3, width * 0.7])
    box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.25, LINE),
                             ("BACKGROUND", (0, 0), (0, -1), GREY), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(box)

    # 3. meter readings
    story.append(Paragraph("Meter readings", section))
    meter = [
        [P("Reading", cell_b), P("Previous read", cell_b), P("Current read", cell_b), P("Difference", cell_b)],
        [P("KWH"), P(fmt_qty(inp.previous_kwh)), P(fmt_qty(inp.current_kwh)), P(fmt_qty(result.kwh_diff))],
        [P("KVAH"), P(fmt_qty(inp.previous_kvah)), P(fmt_qty(inp.current_kvah)), P(fmt_qty(result.kvah_diff))],
        [P("Solar export (net metering)"), P("-"), P("-"), P(fmt_qty(result.solar_export_units))],
        [P(f"Billed units (net of solar, {result.billing_basis} basis)", cell_b), "", "", P(fmt_qty(result.billed_units), cell_b)],
    ]
    mt = Table(meter, colWidths=[width * 0.4, width * 0.2, width * 0.2, width * 0.2], repeatRows=1)
    mt.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, LINE), ("BACKGROUND", (0, 0), (-1, 0), GREY),
                            ("BACKGROUND", (0, -1), (-1, -1), HILITE), ("SPAN", (0, -1), (2, -1)),
                            ("ALIGN", (1, 0), (-1, -1), "RIGHT")]))
    story.append(mt)

    # 4. charges
    story.append(Paragraph("Charges", section))
    data = [[P("Description", cell_b), P("Qty", cell_b), P("Rate", cell_b), P("Amount (Rs)", cell_b)]]
    cmds = [("GRID", (0, 0), (-1, -1), 0.25, LINE), ("BACKGROUND", (0, 0), (-1, 0), GREY),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    for i, row in enumerate(statement_rows(result), start=1):
        st = cell_b if row.style in ("bold", "highlight") else cell
        data.append([P(row.description, st), P(row.qty, st), P(row.rate, st), P(row.amount, st)])
        if row.style == "highlight":
            cmds.append(("BACKGROUND", (0, i), (-1, i), HILITE))
    ct = Table(data, colWidths=[width * 0.52, width * 0.14, width * 0.14, width * 0.20], repeatRows=1)
    ct.setStyle(TableStyle(cmds))
    story.append(ct)

    # 5. notes + footer
    if result.notes:
        story.append(Paragraph("Notes", section))
        for n in result.notes:
            story.append(Paragraph("&bull; " + escape(n), cell))
    story.append(Paragraph("This is a computer-generated bill prepared with BillGen UP. "
                           "Verify rates against the official tariff order.", foot))
    doc.build(story)
    buf.seek(0)
    return buf
