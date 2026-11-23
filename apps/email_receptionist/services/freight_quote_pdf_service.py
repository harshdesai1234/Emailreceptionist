"""
FreightQuotePDFService — Generates clean, branded official PDF freight quotations.
"""
import io
import logging
from datetime import datetime
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

logger = logging.getLogger(__name__)


class FreightQuotePDFService:

    @classmethod
    def generate_quote_pdf(
        cls,
        quote_number: str,
        company_name: str,
        customer_name: str,
        customer_company: str,
        origin_city: str,
        destination_city: str,
        vehicle_type: str,
        base_rate: float,
        transit_time: str,
        detention_charge: float = 1500.0,
        cargo_type: str = 'General Cargo',
        weight: str = 'Standard Load',
        terms: str = ''
    ) -> bytes:
        """
        Builds a formal logistics freight quotation PDF and returns the raw bytes.
        """
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=36,
            leftMargin=36,
            topMargin=36,
            bottomMargin=36
        )

        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            'TitleStyle',
            parent=styles['Heading1'],
            fontSize=18,
            leading=22,
            textColor=colors.HexColor('#0f172a'),
            fontName='Helvetica-Bold'
        )
        subtitle_style = ParagraphStyle(
            'SubtitleStyle',
            parent=styles['Normal'],
            fontSize=10,
            leading=13,
            textColor=colors.HexColor('#64748b'),
            fontName='Helvetica'
        )
        bold_cell = ParagraphStyle(
            'BoldCell',
            parent=styles['Normal'],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor('#0f172a'),
            fontName='Helvetica-Bold'
        )
        norm_cell = ParagraphStyle(
            'NormCell',
            parent=styles['Normal'],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor('#334155'),
            fontName='Helvetica'
        )

        elements = []

        # 1. Header & Title Banner
        header_data = [
            [
                Paragraph(f"<b>{company_name or 'FREIGHT LOGISTICS & FLEET'}</b>", title_style),
                Paragraph(f"<b>OFFICIAL FREIGHT QUOTATION</b><br/><font color='#64748b'>Quote #: {quote_number}<br/>Date: {datetime.now().strftime('%d-%b-%Y')}</font>", ParagraphStyle('HRight', parent=bold_cell, alignment=2))
            ]
        ]
        header_table = Table(header_data, colWidths=[4.0 * inch, 3.2 * inch])
        header_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        elements.append(header_table)
        elements.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#E10600"), spaceBefore=8, spaceAfter=14))

        # 2. Bill-To & Route Summary Box
        info_data = [
            [
                Paragraph("<b>PREPARED FOR:</b>", bold_cell),
                Paragraph("<b>LANE & TRANSIT SPECIFICATIONS:</b>", bold_cell)
            ],
            [
                Paragraph(f"{customer_name or 'Valued Client'}<br/>{customer_company or 'Direct Procurement'}", norm_cell),
                Paragraph(f"<b>Route:</b> {origin_city} ➔ {destination_city}<br/><b>Transit Time:</b> {transit_time or 'Standard'}", norm_cell)
            ],
            [
                Paragraph("", norm_cell),
                Paragraph(f"<b>Cargo & Weight:</b> {cargo_type} ({weight})", norm_cell)
            ]
        ]
        info_table = Table(info_data, colWidths=[3.6 * inch, 3.6 * inch])
        info_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#e2e8f0')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#f1f5f9')),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
        elements.append(info_table)
        elements.append(Spacer(1, 14))

        # 3. Freight Cost Breakdown Table
        gst_amount = base_rate * 0.05
        total_estimate = base_rate + gst_amount

        pricing_data = [
            [Paragraph("<b>Item / Charge Description</b>", bold_cell), Paragraph("<b>Vehicle Spec</b>", bold_cell), Paragraph("<b>Rate (INR)</b>", ParagraphStyle('HRightHead', parent=bold_cell, alignment=2))],
            [Paragraph("<b>Base Freight Transportation Charges</b><br/><font color='#64748b' size='7.5'>Direct point-to-point dispatch</font>", norm_cell), Paragraph(str(vehicle_type or 'FTL Vehicle'), norm_cell), Paragraph(f"₹ {base_rate:,.2f}", ParagraphStyle('HRightVal', parent=norm_cell, alignment=2))],
            [Paragraph("Toll & Highway Green Cess Charges", norm_cell), Paragraph("Standard Route", norm_cell), Paragraph("INCLUDED", ParagraphStyle('HRightVal', parent=norm_cell, alignment=2))],
            [Paragraph("GST (Under Reverse Charge Mechanism / 5%)", norm_cell), Paragraph("5% RCM", norm_cell), Paragraph(f"₹ {gst_amount:,.2f}", ParagraphStyle('HRightVal', parent=norm_cell, alignment=2))],
            [Paragraph("<b>ESTIMATED TRIP TOTAL (INR)</b>", bold_cell), Paragraph("", norm_cell), Paragraph(f"<b>₹ {total_estimate:,.2f}</b>", ParagraphStyle('TotalVal', parent=bold_cell, alignment=2, textColor=colors.HexColor('#0284c7')))],
        ]
        pricing_table = Table(pricing_data, colWidths=[3.8 * inch, 2.0 * inch, 1.4 * inch])
        pricing_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f1f5f9')),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#cbd5e1')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#eff6ff')),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
        elements.append(pricing_table)
        elements.append(Spacer(1, 14))

        # 4. Standard Commercial Terms & Detention Clause
        elements.append(Paragraph("<b>Standard Commercial & Operating Terms:</b>", bold_cell))
        standard_terms = (
            f"1. <b>Detention Policy:</b> 24 hours allowed for loading and 24 hours for unloading. Thereafter, detention applies at ₹{detention_charge:,.2f} per day.<br/>"
            f"2. <b>Insurance:</b> Transit insurance to be covered by the consignor/consignee under marine cargo policy.<br/>"
            f"3. <b>Validity:</b> This freight quote is valid for 7 calendar days from issuance.<br/>"
            f"4. <b>E-Way Bill:</b> Valid GST E-Way Bill must be provided prior to truck departure.<br/>"
            f"5. <i>{terms or 'Payment terms: 90% against LR copy, balance 10% on clear POD submission.'}</i>"
        )
        elements.append(Paragraph(standard_terms, ParagraphStyle('TermsStyle', parent=norm_cell, leading=14, textColor=colors.HexColor('#475569'))))
        elements.append(Spacer(1, 20))

        # 5. Sign-off Footer
        footer_text = f"Generated by {company_name or 'Agentyne Logistics System'} · AI Automated Email Dispatch"
        elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cbd5e1"), spaceBefore=10, spaceAfter=6))
        elements.append(Paragraph(footer_text, ParagraphStyle('FootStyle', parent=subtitle_style, alignment=1, fontSize=8)))

        doc.build(elements)
        buffer.seek(0)
        return buffer.getvalue()
