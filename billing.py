"""
Invoice generation: groups RawCharges by client, applies markup, creates Invoice + InvoiceLines.
PDF export via fpdf2.
"""
from datetime import date, timedelta
from collections import defaultdict
from fpdf import FPDF
import io

CATEGORY_ORDER = ['Calls', 'SIP Trunks', 'Broadband', 'Leased Lines', 'WLR', 'Inbound', 'Other']

FILE_TYPE_TO_CATEGORY = {
    'gamma_calls_sip':   'Calls',
    'gamma_calls_div':   'Calls',
    'gamma_calls_ftc':   'Calls',
    'gamma_calls_ibrs':  'Calls',
    'gamma_calls_nts':   'Calls',
    'nasstar_cdr':       'Calls',
    'gamma_ipdc':        'SIP Trunks',
    'gamma_bb':          'Broadband',
    'gamma_ces':         'Leased Lines',
    'gamma_wlr':         'WLR',
    'gamma_inb':         'Inbound',
}


def generate_invoices(billing_period, client_ids, session, run_id, created_by, settings):
    from models import RawCharge, Invoice, InvoiceLine, ImportBatch, Client
    invoices_created = []

    for client_id in client_ids:
        client = session.get(Client, client_id)
        if not client:
            continue

        charges = (session.query(RawCharge)
                   .join(ImportBatch, RawCharge.batch_id == ImportBatch.id)
                   .filter(RawCharge.client_id == client_id,
                           RawCharge.invoiced == False,
                           RawCharge.archived == False)
                   .all())

        # Use per-client markup, fall back to global default
        markup = 1.0 + (client.markup_pct / 100.0)
        vat = settings.default_vat_rate / 100.0

        groups = defaultdict(lambda: {'cost': 0.0, 'qty': 0, 'charge_ids': []})
        for c in charges:
            from models import ImportBatch as IB
            batch = session.get(IB, c.batch_id)
            cat = FILE_TYPE_TO_CATEGORY.get(batch.file_type if batch else '', 'Other')
            net_cost = c.cost_amount * c.quantity - c.credit_amount
            key = (cat, c.product_name or 'Service')
            groups[key]['cost'] += net_cost
            groups[key]['qty'] += c.quantity
            groups[key]['charge_ids'].append(c.id)

        # Merge all call lines into a single "Call Charges" line
        call_total_cost = sum(g['cost'] for (cat, _), g in groups.items() if cat == 'Calls')
        call_charge_ids = [cid for (cat, _), g in groups.items() if cat == 'Calls' for cid in g['charge_ids']]
        non_call_groups = {k: v for k, v in groups.items() if k[0] != 'Calls'}

        lines = []
        sort_order = 0

        # Add single call charges line if there are any calls
        if call_charge_ids:
            call_sell = round(call_total_cost * markup, 2)
            lines.append({
                'category': 'Calls',
                'description': 'Call Charges',
                'quantity': 1,
                'unit_cost': round(call_total_cost, 2),
                'unit_price': call_sell,
                'line_total': call_sell,
                'vat_rate': 20.0,
                'sort_order': sort_order,
                'charge_ids': call_charge_ids,
            })
            sort_order += 1

        for cat in CATEGORY_ORDER:
            if cat == 'Calls':
                continue
            for (g_cat, g_name), g in sorted(non_call_groups.items()):
                if g_cat != cat:
                    continue
                unit_cost = round(g['cost'], 2)
                unit_price = round(unit_cost * markup, 2)
                lines.append({
                    'category': cat,
                    'description': g_name,
                    'quantity': 1,
                    'unit_cost': unit_cost,
                    'unit_price': unit_price,
                    'line_total': unit_price,
                    'vat_rate': 20.0,
                    'sort_order': sort_order,
                    'charge_ids': g['charge_ids'],
                })
                sort_order += 1

        # Add recurring fixed charges (IP addressing, SIP channels etc)
        from models import RecurringCharge, ClientIdentifier
        for rc in RecurringCharge.query.filter_by(client_id=client_id, active=True).all():
            lines.append({
                'category': rc.category,
                'description': rc.description,
                'quantity': 1,
                'unit_cost': rc.unit_cost,
                'unit_price': rc.unit_price,
                'line_total': rc.unit_price,
                'vat_rate': rc.vat_rate,
                'sort_order': sort_order,
                'charge_ids': [],
            })
            sort_order += 1

        # Auto-add £24 IP addressing for leased line clients (if not already a recurring charge)
        has_ces = ClientIdentifier.query.filter_by(
            client_id=client_id, id_type='gamma_ces_circuit', active=True).first()
        has_ip_charge = any('IP' in l['description'].upper() for l in lines)
        if has_ces and not has_ip_charge:
            lines.append({
                'category': 'Leased Lines',
                'description': 'IP Addressing',
                'quantity': 1,
                'unit_cost': 0.0,
                'unit_price': 24.00,
                'line_total': 24.00,
                'vat_rate': 20.0,
                'sort_order': sort_order,
                'charge_ids': [],
            })
            sort_order += 1

        # Always generate an invoice — zero line if nothing to bill
        if not lines:
            lines.append({
                'category': 'Other',
                'description': 'Hosted Voice & Communications Services',
                'quantity': 1,
                'unit_cost': 0.0,
                'unit_price': 0.0,
                'line_total': 0.0,
                'vat_rate': 20.0,
                'sort_order': 0,
                'charge_ids': [],
            })

        subtotal = round(sum(l['line_total'] for l in lines), 2)
        vat_amount = round(subtotal * vat, 2)
        total = round(subtotal + vat_amount, 2)

        inv_num = f"{settings.invoice_prefix}{settings.next_invoice_number:05d}"
        settings.next_invoice_number += 1

        inv_date = date.today()
        due = inv_date + timedelta(days=settings.payment_terms_days)

        inv = Invoice(
            run_id=run_id,
            client_id=client_id,
            invoice_number=inv_num,
            invoice_date=inv_date,
            due_date=due,
            billing_period=billing_period,
            subtotal=subtotal,
            vat_amount=vat_amount,
            total=total,
            status='draft',
        )
        session.add(inv)
        session.flush()

        for l in lines:
            il = InvoiceLine(
                invoice_id=inv.id,
                category=l['category'],
                description=l['description'],
                quantity=l['quantity'],
                unit_cost=l['unit_cost'],
                unit_price=l['unit_price'],
                line_total=l['line_total'],
                vat_rate=l['vat_rate'],
                sort_order=l['sort_order'],
            )
            session.add(il)
            session.flush()
            from models import RawCharge as _RC
            for cid in l['charge_ids']:
                rc = session.get(_RC, cid)
                if rc:
                    rc.invoiced = True
                    rc.invoice_line_id = il.id

        invoices_created.append(inv)

    session.commit()
    return invoices_created


# ── PDF Generation ────────────────────────────────────────────────────────────

def generate_pdf(invoice, settings):
    """Generate a PDF invoice. Returns bytes."""
    client = invoice.client

    pdf = FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # ── Header ────────────────────────────────────────────────────────────────
    pdf.set_fill_color(255, 255, 255)
    pdf.rect(0, 0, 210, 40, 'F')

    # Logo top-left
    import os
    logo_path = os.path.join(os.path.dirname(__file__), 'static', 'synthesis-logo.jpg')
    if os.path.exists(logo_path):
        pdf.image(logo_path, x=15, y=8, h=18)

    # Company address top-right
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_text_color(30, 58, 95)
    pdf.set_xy(110, 8)
    pdf.cell(85, 5, settings.company_name or 'Synthesis IT Limited', align='R', ln=True)
    pdf.set_font('Helvetica', '', 8)
    pdf.set_text_color(80, 80, 80)
    addr_parts = [p for p in [settings.address_line1, settings.address_line2, settings.city, settings.postcode] if p]
    for part in addr_parts:
        pdf.set_x(110)
        pdf.cell(85, 4, part, align='R', ln=True)
    if settings.phone:
        pdf.set_x(110)
        pdf.cell(85, 4, f"Tel: {settings.phone}", align='R', ln=True)
    if settings.website:
        pdf.set_x(110)
        pdf.cell(85, 4, settings.website, align='R', ln=True)

    # Divider line
    pdf.set_draw_color(30, 58, 95)
    pdf.set_line_width(0.5)
    pdf.line(15, 38, 195, 38)

    # ── Bill To / Invoice Details ─────────────────────────────────────────────
    pdf.set_text_color(30, 58, 95)
    pdf.set_xy(15, 44)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_fill_color(240, 244, 248)
    pdf.cell(85, 6, 'BILL TO', fill=True)
    pdf.set_x(110)
    pdf.cell(85, 6, 'INVOICE DETAILS', fill=True, ln=True)

    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_text_color(20, 20, 20)
    pdf.set_xy(15, 50)
    pdf.cell(0, 6, client.name, ln=True)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(80, 80, 80)
    if client.account_ref:
        pdf.set_x(15)
        pdf.cell(0, 4, f"Account: {client.account_ref}", ln=True)
    for part in [client.address_line1, client.address_line2, client.city, client.postcode]:
        if part:
            pdf.set_x(15)
            pdf.cell(0, 4, part, ln=True)

    # Right side details
    details = [
        ('Period:', invoice.billing_period or ''),
        ('Due Date:', invoice.due_date.strftime('%d %b %Y') if invoice.due_date else ''),
        ('Account Ref:', client.account_ref or ''),
        ('VAT No:', settings.vat_number or ''),
    ]
    dy = 50
    pdf.set_font('Helvetica', '', 9)
    for label, val in details:
        pdf.set_xy(110, dy)
        pdf.set_font('Helvetica', 'B', 9)
        pdf.cell(35, 4, label)
        pdf.set_font('Helvetica', '', 9)
        pdf.cell(50, 4, val, ln=True)
        dy += 5

    # ── Line Items Table ──────────────────────────────────────────────────────
    y = max(pdf.get_y() + 6, 85)
    pdf.set_xy(15, y)
    pdf.set_fill_color(30, 58, 95)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.cell(90, 7, 'Description', fill=True)
    pdf.cell(25, 7, 'Category', fill=True, align='C')
    pdf.cell(25, 7, 'Net Cost', fill=True, align='R')
    pdf.cell(25, 7, 'Price (ex VAT)', fill=True, align='R')
    pdf.cell(20, 7, 'VAT %', fill=True, align='R', ln=True)

    pdf.set_text_color(20, 20, 20)
    pdf.set_font('Helvetica', '', 9)
    fill = False
    for line in sorted(invoice.lines, key=lambda l: (l.sort_order,)):
        if pdf.get_y() > 260:
            pdf.add_page()
        pdf.set_fill_color(248, 250, 252) if fill else pdf.set_fill_color(255, 255, 255)
        pdf.set_x(15)
        pdf.cell(90, 6, line.description[:55], fill=True)
        pdf.cell(25, 6, line.category, fill=True, align='C')
        pdf.cell(25, 6, f"£{line.unit_cost:,.2f}", fill=True, align='R')
        pdf.cell(25, 6, f"£{line.line_total:,.2f}", fill=True, align='R')
        pdf.cell(20, 6, f"{line.vat_rate:.0f}%", fill=True, align='R', ln=True)
        fill = not fill

    # ── Totals ────────────────────────────────────────────────────────────────
    pdf.ln(2)
    y = pdf.get_y()
    pdf.set_x(130)
    pdf.set_font('Helvetica', '', 9)
    for label, val in [
        ('Subtotal (ex VAT)', f"£{invoice.subtotal:,.2f}"),
        (f"VAT @ {settings.default_vat_rate:.0f}%", f"£{invoice.vat_amount:,.2f}"),
    ]:
        pdf.set_x(130)
        pdf.cell(45, 6, label, align='R')
        pdf.cell(30, 6, val, align='R', ln=True)

    pdf.set_fill_color(30, 58, 95)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Helvetica', 'B', 11)
    pdf.set_x(130)
    pdf.cell(45, 8, 'TOTAL (inc VAT)', fill=True, align='R')
    pdf.cell(30, 8, f"£{invoice.total:,.2f}", fill=True, align='R', ln=True)

    # ── Bank / Payment Details ────────────────────────────────────────────────
    if settings.bank_account:
        pdf.ln(6)
        pdf.set_text_color(30, 58, 95)
        pdf.set_font('Helvetica', 'B', 9)
        pdf.set_x(15)
        pdf.set_fill_color(240, 244, 248)
        pdf.cell(180, 6, 'PAYMENT DETAILS', fill=True, ln=True)
        pdf.set_font('Helvetica', '', 9)
        pdf.set_text_color(40, 40, 40)
        pdf.set_x(15)
        bank_line = f"{settings.bank_name or ''}  |  Sort Code: {settings.bank_sort_code or ''}  |  Account: {settings.bank_account}"
        pdf.cell(0, 5, bank_line.strip(' |'), ln=True)
        pdf.set_x(15)
        pdf.cell(0, 5, f"Payment due within {settings.payment_terms_days} days of invoice date. Please quote invoice number {invoice.invoice_number}.", ln=True)

    # ── Footer ────────────────────────────────────────────────────────────────
    pdf.set_y(-15)
    pdf.set_font('Helvetica', 'I', 7)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 5, f"{settings.company_name}  |  {settings.company_number or ''}  |  VAT {settings.vat_number or ''}  |  All prices in GBP", align='C')

    # ── Itemised Calls Appendix ───────────────────────────────────────────────
    call_charges = []
    try:
        from models import RawCharge as _RC
        from flask import current_app
        from models import db as _db
        for line in invoice.lines:
            if line.category == 'Calls':
                calls = (_db.session.query(_RC)
                         .filter(_RC.invoice_line_id == line.id,
                                 _RC.charge_type == 'Call')
                         .order_by(_RC.call_date, _RC.description)
                         .all())
                call_charges.extend(calls)
    except Exception:
        call_charges = []

    if call_charges:
        pdf.add_page()

        # Header
        pdf.set_fill_color(30, 58, 95)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font('Helvetica', 'B', 11)
        pdf.set_xy(15, 15)
        pdf.cell(180, 8, f"Itemised Calls - {invoice.invoice_number} - {client.name}", fill=True, ln=True)

        # Column headers
        pdf.set_fill_color(240, 244, 248)
        pdf.set_text_color(30, 58, 95)
        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_x(15)
        pdf.cell(22, 6, 'Date', fill=True)
        pdf.cell(18, 6, 'Time', fill=True)
        pdf.cell(35, 6, 'Called Number', fill=True)
        pdf.cell(70, 6, 'Description', fill=True)
        pdf.cell(18, 6, 'Duration', fill=True, align='R')
        pdf.cell(22, 6, 'Cost', fill=True, align='R', ln=True)

        pdf.set_font('Helvetica', '', 8)
        pdf.set_text_color(40, 40, 40)
        fill = False
        for c in call_charges:
            if pdf.get_y() > 270:
                pdf.add_page()
                pdf.set_fill_color(30, 58, 95)
                pdf.set_text_color(255, 255, 255)
                pdf.set_font('Helvetica', 'B', 11)
                pdf.set_xy(15, 15)
                pdf.cell(180, 8, f"Itemised Calls (continued) - {invoice.invoice_number}", fill=True, ln=True)
                pdf.set_font('Helvetica', '', 8)
                pdf.set_text_color(40, 40, 40)

            pdf.set_fill_color(248, 250, 252) if fill else pdf.set_fill_color(255, 255, 255)
            date_str = c.call_date.strftime('%d/%m/%Y') if c.call_date else '-'
            time_str = c.description or '-'
            dest = c.destination or '-'
            desc = (c.product_name or '').replace('Call - ', '')[:35]
            dur = f"{c.call_duration // 60}m {c.call_duration % 60:02d}s" if c.call_duration else '-'
            cost = f"£{c.cost_amount:.4f}"

            pdf.set_x(15)
            pdf.cell(22, 5, date_str, fill=True)
            pdf.cell(18, 5, time_str, fill=True)
            pdf.cell(35, 5, dest, fill=True)
            pdf.cell(70, 5, desc, fill=True)
            pdf.cell(18, 5, dur, fill=True, align='R')
            pdf.cell(22, 5, cost, fill=True, align='R', ln=True)
            fill = not fill

    return pdf.output()
