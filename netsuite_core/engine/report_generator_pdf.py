"""
CFO PDF uses only scalar Python inputs (no Polars). Numeric report fields must be
pre-aggregated in financial_math; do not pass raw DataFrame cells here (avoids list[str] in strings).
"""
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def generate_cfo_pdf(
    grade: str,
    total_capital_deployed: float,
    estimated_turnover_ratio: float,
    dead_val: float,
    integrity_qty: int,
    integrity_item_count: int,
    transfer_item_count: int,
    neg_item_count: int,
    weekly_otb_limit: float,
    total_draft_po_amount: float,
    cash_collision: bool,
    loss_item_count: int,
    cost_variance_item_count: int,
) -> io.BytesIO:
    """
    One-page in-memory CFO strategic brief (Data Health Scan executive summary).
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40,
    )
    elements = []

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleStyle",
        parent=styles["Heading1"],
        fontSize=16,
        spaceAfter=6,
        textColor=colors.HexColor("#1A1A1A"),
        fontName="Helvetica-Bold",
    )
    subtitle_style = ParagraphStyle(
        "SubtitleStyle",
        parent=styles["Normal"],
        fontSize=9,
        spaceAfter=16,
        textColor=colors.HexColor("#555555"),
        fontName="Helvetica",
    )
    header_style = ParagraphStyle(
        "HeaderStyle",
        parent=styles["Heading2"],
        fontSize=11,
        spaceAfter=6,
        textColor=colors.HexColor("#2A2A2A"),
        fontName="Helvetica-Bold",
    )
    body_style = ParagraphStyle(
        "BodyStyle",
        parent=styles["Normal"],
        fontSize=9,
        spaceAfter=8,
        leading=12,
        textColor=colors.HexColor("#1A1A1A"),
        fontName="Helvetica",
    )
    alert_style = ParagraphStyle(
        "AlertStyle",
        parent=styles["Normal"],
        fontSize=9,
        spaceAfter=8,
        leading=12,
        textColor=colors.HexColor("#8B0000"),
        fontName="Helvetica-Bold",
    )

    elements.append(Paragraph("GOVNR SYSTEMS INC.", title_style))
    elements.append(Paragraph("CFO STRATEGIC BRIEF — DATA HEALTH SCAN", title_style))
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elements.append(
        Paragraph(f"Generated: {timestamp_str} | Audience: Chief Financial Officer", subtitle_style)
    )

    elements.append(Paragraph("I. FINANCIAL PERFORMANCE", header_style))
    summary_data = [
        ["System grade:", grade],
        ["Total Capital Deployed:", f"${total_capital_deployed:,.2f}"],
        ["Inventory Velocity (Turnover):", f"{estimated_turnover_ratio:.2f}x"],
    ]
    t_summary = Table(summary_data, colWidths=[160, 340])
    t_summary.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1A1A1A")),
            ]
        )
    )
    elements.append(t_summary)
    elements.append(Spacer(1, 10))

    elements.append(Paragraph("II. SAFE SPENDING LIMIT & CASH COLLISION", header_style))
    elements.append(
        Paragraph(
            f"<b>Weekly OTB limit:</b> ${weekly_otb_limit:,.2f}<br/>"
            f"<b>Draft PO total (rolling 7 days):</b> ${total_draft_po_amount:,.2f}<br/>"
            "<i>Compared to your CFO-approved weekly ceiling using location-first capital math.</i>",
            body_style,
        )
    )
    if cash_collision:
        elements.append(
            Paragraph(
                "<b>Cash Collision Warning:</b> Pending purchase orders in the rolling window exceed your safe spending limit.",
                alert_style,
            )
        )
    else:
        elements.append(Paragraph("<b>Status:</b> Draft PO exposure is within the weekly OTB limit.", body_style))
    elements.append(Spacer(1, 6))

    elements.append(Paragraph("III. DEAD STOCK", header_style))
    elements.append(
        Paragraph(
            f"<b>Dead stock (non-performing capital):</b> ${dead_val:,.2f}<br/>"
            "<i>Zero trailing annual sold, no replacement demand, extended dormancy, with physical on-hand.</i>",
            body_style,
        )
    )
    elements.append(Spacer(1, 6))

    elements.append(Paragraph("IV. WAREHOUSE TRANSFER ALERTS", header_style))
    if transfer_item_count > 0:
        elements.append(
            Paragraph(
                f"<b>Flagged items:</b> {transfer_item_count} — one location under ~15 days supply while another exceeds ~180 days.",
                alert_style,
            )
        )
    else:
        elements.append(Paragraph("<b>Status:</b> No transfer pattern detected on current thresholds.", body_style))
    elements.append(Spacer(1, 6))

    elements.append(Paragraph("V. SELLING AT A LOSS", header_style))
    elements.append(
        Paragraph(
            f"<b>Distinct items at or below average cost vs base price (with on-hand):</b> {loss_item_count}.<br/>"
            "<i>Requires audit for intentional loss-leaders vs data errors.</i>",
            body_style,
        )
    )
    elements.append(Spacer(1, 6))

    elements.append(Paragraph("VI. MISSING / $0 COST ALERT", header_style))
    elements.append(
        Paragraph(
            f"<b>Units on-hand with non-positive average cost:</b> {integrity_qty:,} across {integrity_item_count} items.<br/>"
            "<i>Margin reporting for these identifiers is not reliable until ERP cost is corrected.</i>",
            body_style,
        )
    )
    elements.append(Spacer(1, 6))

    elements.append(Paragraph("VII. INTERBRANCH COST DIFFERENCE", header_style))
    if cost_variance_item_count > 0:
        elements.append(
            Paragraph(
                f"<b>Flagged items:</b> {cost_variance_item_count} with &gt;25% spread between min and max location average cost.",
                alert_style,
            )
        )
    else:
        elements.append(Paragraph("<b>Status:</b> No material inter-location average cost spread detected.", body_style))
    elements.append(Spacer(1, 6))

    elements.append(Paragraph("VIII. NEGATIVE INVENTORY", header_style))
    elements.append(
        Paragraph(
            f"<b>Distinct items with negative computed on-hand (pre-policy):</b> {neg_item_count}.",
            body_style,
        )
    )

    doc.build(elements)
    buffer.seek(0)
    return buffer
