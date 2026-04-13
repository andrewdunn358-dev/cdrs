import os
from flask import (Flask, render_template, redirect, url_for, request,
                   flash, session, send_file, jsonify, abort)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from datetime import date, datetime
import io

from models import (db, User, Client, ClientIdentifier, ImportBatch,
                    RawCharge, Invoice, InvoiceLine, InvoiceRun, CompanySettings)
from importer import parse_file, match_charges_to_clients
from billing import generate_invoices, generate_pdf

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'synthesis-billing-secret-change-me')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///billing.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB

db.init_app(app)

# Jinja2 globals
@app.context_processor
def inject_globals():
    from datetime import date
    return {'today': date.today()}
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def get_settings():
    s = CompanySettings.query.first()
    if not s:
        s = CompanySettings(company_name='Synthesis IT')
        db.session.add(s)
        db.session.commit()
    return s


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.check_password(request.form['password']):
            login_user(user)
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Invalid username or password', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    s = get_settings()
    total_clients = Client.query.filter_by(active=True).count()
    total_invoices = Invoice.query.count()
    unpaid = Invoice.query.filter(Invoice.status.in_(['draft','sent'])).all()
    unpaid_total = sum(i.total for i in unpaid)
    recent_imports = ImportBatch.query.order_by(ImportBatch.imported_at.desc()).limit(5).all()
    unmatched = RawCharge.query.filter_by(matched=False, invoiced=False).count()
    recent_invoices = Invoice.query.order_by(Invoice.created_at.desc()).limit(8).all()
    return render_template('dashboard.html', s=s,
        total_clients=total_clients, total_invoices=total_invoices,
        unpaid_total=unpaid_total, recent_imports=recent_imports,
        unmatched=unmatched, recent_invoices=recent_invoices)


# ── Clients ───────────────────────────────────────────────────────────────────

@app.route('/clients')
@login_required
def clients():
    q = request.args.get('q', '')
    query = Client.query.filter_by(active=True)
    if q:
        query = query.filter(Client.name.ilike(f'%{q}%') | Client.account_ref.ilike(f'%{q}%'))
    clients = query.order_by(Client.name).all()
    return render_template('clients/list.html', clients=clients, q=q)

@app.route('/clients/new', methods=['GET', 'POST'])
@login_required
def client_new():
    if request.method == 'POST':
        c = Client(
            name=request.form['name'],
            address_line1=request.form.get('address_line1'),
            address_line2=request.form.get('address_line2'),
            city=request.form.get('city'),
            postcode=request.form.get('postcode'),
            contact_name=request.form.get('contact_name'),
            contact_email=request.form.get('contact_email'),
            contact_phone=request.form.get('contact_phone'),
            account_ref=request.form.get('account_ref'),
            vat_number=request.form.get('vat_number'),
            markup_pct=float(request.form.get('markup_pct', 30.0)),
            billing_day=int(request.form.get('billing_day', 1)),
            notes=request.form.get('notes'),
        )
        db.session.add(c)
        db.session.commit()
        flash(f'Client "{c.name}" created.', 'success')
        return redirect(url_for('client_detail', id=c.id))
    return render_template('clients/form.html', client=None)

@app.route('/clients/<int:id>')
@login_required
def client_detail(id):
    from collections import defaultdict
    c = db.get_or_404(Client, id)
    invoices = Invoice.query.filter_by(client_id=id).order_by(Invoice.created_at.desc()).limit(20).all()

    # Get all uninvoiced charges
    all_charges = RawCharge.query.filter_by(client_id=id, invoiced=False).all()

    # Group by period + category + product for summary
    summary = defaultdict(lambda: {'cost': 0.0, 'qty': 0, 'charge_type': ''})
    for ch in all_charges:
        batch = db.session.get(ImportBatch, ch.batch_id)
        file_type = batch.file_type if batch else ''
        # Summarise calls by destination type, rentals by product name
        if ch.charge_type == 'Call':
            key = (ch.billing_period, 'Call', ch.product_name or 'Call')
        else:
            key = (ch.billing_period, ch.charge_type, ch.product_name or 'Service')
        summary[key]['cost'] += ch.cost_amount * ch.quantity - ch.credit_amount
        summary[key]['qty'] += 1
        summary[key]['charge_type'] = ch.charge_type

    # Convert to list sorted by period desc, then charge type, then product
    charge_summary = []
    for (period, ctype, product), vals in sorted(summary.items(), key=lambda x: (x[0][0], x[0][1], x[0][2])):
        sell = vals['cost'] * (1 + c.markup_pct / 100)
        charge_summary.append({
            'period': period,
            'charge_type': ctype,
            'product': product,
            'qty': vals['qty'],
            'cost': vals['cost'],
            'sell': sell,
        })

    # Period totals
    period_totals = defaultdict(lambda: {'cost': 0.0, 'sell': 0.0})
    for cs in charge_summary:
        period_totals[cs['period']]['cost'] += cs['cost']
        period_totals[cs['period']]['sell'] += cs['sell']

    return render_template('clients/detail.html', client=c, invoices=invoices,
                           charge_summary=charge_summary, period_totals=dict(period_totals))

@app.route('/clients/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def client_edit(id):
    c = db.get_or_404(Client, id)
    if request.method == 'POST':
        c.name = request.form['name']
        c.address_line1 = request.form.get('address_line1')
        c.address_line2 = request.form.get('address_line2')
        c.city = request.form.get('city')
        c.postcode = request.form.get('postcode')
        c.contact_name = request.form.get('contact_name')
        c.contact_email = request.form.get('contact_email')
        c.contact_phone = request.form.get('contact_phone')
        c.account_ref = request.form.get('account_ref')
        c.vat_number = request.form.get('vat_number')
        c.markup_pct = float(request.form.get('markup_pct', 30.0))
        c.billing_day = int(request.form.get('billing_day', 1))
        c.notes = request.form.get('notes')
        db.session.commit()
        flash('Client updated.', 'success')
        return redirect(url_for('client_detail', id=id))
    return render_template('clients/form.html', client=c)

@app.route('/clients/<int:id>/identifiers/add', methods=['POST'])
@login_required
def add_identifier(id):
    db.get_or_404(Client, id)
    val = request.form.get('id_value', '').strip()
    if val:
        existing = ClientIdentifier.query.filter_by(
            id_type=request.form['id_type'], id_value=val).first()
        if existing:
            flash(f'Identifier "{val}" already assigned to another client.', 'warning')
        else:
            ident = ClientIdentifier(
                client_id=id,
                id_type=request.form['id_type'],
                id_value=val,
                description=request.form.get('description', ''),
            )
            db.session.add(ident)
            db.session.commit()
            flash('Identifier added.', 'success')
    return redirect(url_for('client_detail', id=id))

@app.route('/identifiers/<int:id>/delete', methods=['POST'])
@login_required
def delete_identifier(id):
    ident = db.get_or_404(ClientIdentifier, id)
    client_id = ident.client_id
    db.session.delete(ident)
    db.session.commit()
    flash('Identifier removed.', 'success')
    return redirect(url_for('client_detail', id=client_id))


# ── Imports ───────────────────────────────────────────────────────────────────

@app.route('/imports')
@login_required
def imports():
    batches = ImportBatch.query.order_by(ImportBatch.imported_at.desc()).all()
    return render_template('imports/list.html', batches=batches)

@app.route('/imports/upload', methods=['GET', 'POST'])
@login_required
def import_upload():
    if request.method == 'POST':
        files = request.files.getlist('files')
        results = []
        for f in files:
            if not f.filename:
                continue
            try:
                content = f.read().decode('utf-8-sig', errors='replace')
                file_type, period, records = parse_file(f.filename, content)

                batch = ImportBatch(
                    filename=f.filename,
                    file_type=file_type,
                    billing_period=period,
                    imported_by=current_user.id,
                    record_count=len(records),
                )
                db.session.add(batch)
                db.session.flush()

                charge_objs = []
                for r in records:
                    c = RawCharge(
                        batch_id=batch.id,
                        billing_period=period or r.get('billing_period', ''),
                        **{k: v for k, v in r.items()
                           if k in ('source_key','product_name','charge_type','call_date',
                                    'call_duration','destination','description',
                                    'cost_amount','credit_amount','quantity','site_name','raw_json')}
                    )
                    db.session.add(c)
                    charge_objs.append(c)

                db.session.flush()
                matched = match_charges_to_clients(charge_objs, db.session)
                batch.matched_count = matched
                db.session.commit()

                results.append({'file': f.filename, 'type': file_type,
                                 'records': len(records), 'matched': matched,
                                 'period': period})
            except Exception as e:
                db.session.rollback()
                results.append({'file': f.filename, 'error': str(e)})

        return render_template('imports/results.html', results=results)
    return render_template('imports/upload.html')

@app.route('/imports/<int:id>/delete', methods=['POST'])
@login_required
def import_delete(id):
    batch = db.get_or_404(ImportBatch, id)
    # Only allow deletion if no charges are invoiced
    invoiced = RawCharge.query.filter_by(batch_id=id, invoiced=True).count()
    if invoiced:
        flash('Cannot delete — some charges in this batch are already invoiced.', 'danger')
        return redirect(url_for('imports'))
    RawCharge.query.filter_by(batch_id=id).delete()
    db.session.delete(batch)
    db.session.commit()
    flash('Import batch deleted.', 'success')
    return redirect(url_for('imports'))


# ── Charges / Matching ────────────────────────────────────────────────────────

@app.route('/charges')
@login_required
def charges():
    from models import IgnoredKey
    from sqlalchemy import func
    period = request.args.get('period', '')
    client_id = request.args.get('client_id', '')
    unmatched_only = request.args.get('unmatched', '')

    # Get ignored keys
    ignored_keys = set(i.source_key for i in IgnoredKey.query.all())

    if unmatched_only:
        # Group unmatched by source key — one row per unknown identifier
        grouped = (db.session.query(
                RawCharge.source_key,
                func.count(RawCharge.id).label('count'),
                func.sum(RawCharge.cost_amount * RawCharge.quantity).label('total_cost'),
                func.min(ImportBatch.billing_period).label('period'),
                func.min(RawCharge.product_name).label('sample_product'),
                func.min(ImportBatch.file_type).label('file_type'),
            )
            .join(ImportBatch, RawCharge.batch_id == ImportBatch.id)
            .filter(RawCharge.matched == False,
                    RawCharge.invoiced == False,
                    ~RawCharge.source_key.in_(ignored_keys) if ignored_keys else True)
            .group_by(RawCharge.source_key)
            .order_by(func.count(RawCharge.id).desc())
            .all())

        clients = Client.query.filter_by(active=True).order_by(Client.name).all()
        periods = db.session.query(ImportBatch.billing_period).distinct().order_by(ImportBatch.billing_period.desc()).all()
        unmatched_count = len(grouped)

        return render_template('charges/unmatched.html',
            grouped=grouped, clients=clients,
            periods=[p[0] for p in periods if p[0]],
            unmatched_count=unmatched_count,
            ignored_keys=ignored_keys)

    query = RawCharge.query.join(ImportBatch, RawCharge.batch_id == ImportBatch.id)
    if period:
        query = query.filter(ImportBatch.billing_period == period)
    if client_id:
        query = query.filter(RawCharge.client_id == int(client_id))

    charges = query.order_by(RawCharge.id.desc()).limit(500).all()
    periods = db.session.query(ImportBatch.billing_period).distinct().order_by(ImportBatch.billing_period.desc()).all()
    clients = Client.query.filter_by(active=True).order_by(Client.name).all()
    unmatched_count = (RawCharge.query
        .filter(RawCharge.matched == False, RawCharge.invoiced == False,
                ~RawCharge.source_key.in_(ignored_keys) if ignored_keys else True)
        .distinct(RawCharge.source_key).count())

    return render_template('charges/list.html',
        charges=charges, periods=[p[0] for p in periods if p[0]],
        clients=clients, selected_period=period,
        selected_client=client_id, unmatched_only=unmatched_only,
        unmatched_count=unmatched_count)

@app.route('/charges/<int:id>/assign', methods=['POST'])
@login_required
def charge_assign(id):
    charge = db.get_or_404(RawCharge, id)
    client_id = request.form.get('client_id')
    if client_id:
        charge.client_id = int(client_id)
        charge.matched = True
        db.session.commit()
        flash('Charge assigned to client.', 'success')
    return redirect(request.referrer or url_for('charges'))

@app.route('/charges/ignore', methods=['POST'])
@login_required
def charge_ignore():
    from models import IgnoredKey
    source_key = request.form.get('source_key')
    if source_key:
        existing = IgnoredKey.query.filter_by(source_key=source_key).first()
        if not existing:
            db.session.add(IgnoredKey(
                source_key=source_key,
                reason=request.form.get('reason', ''),
                ignored_by=current_user.id
            ))
            db.session.commit()
        flash(f'"{source_key}" will no longer appear in unmatched charges.', 'success')
    return redirect(url_for('charges', unmatched='1'))

@app.route('/charges/unignore', methods=['POST'])
@login_required
def charge_unignore():
    from models import IgnoredKey
    source_key = request.form.get('source_key')
    IgnoredKey.query.filter_by(source_key=source_key).delete()
    db.session.commit()
    flash(f'"{source_key}" restored to unmatched charges.', 'success')
    return redirect(url_for('charges', unmatched='1'))

@app.route('/charges/ignored')
@login_required
def charges_ignored():
    from models import IgnoredKey
    ignored = IgnoredKey.query.order_by(IgnoredKey.ignored_at.desc()).all()
    return render_template('charges/ignored.html', ignored=ignored)

@app.route('/charges/bulk-assign', methods=['POST'])
@login_required
def charge_bulk_assign():
    source_key = request.form.get('source_key')
    client_id = int(request.form.get('client_id'))
    create_ident = request.form.get('create_identifier') == '1'
    id_type = request.form.get('id_type', 'gamma_circuit')

    updated = (RawCharge.query
               .filter_by(source_key=source_key, matched=False)
               .update({'client_id': client_id, 'matched': True}))

    if create_ident and source_key:
        existing = ClientIdentifier.query.filter_by(id_type=id_type, id_value=source_key).first()
        if not existing:
            db.session.add(ClientIdentifier(
                client_id=client_id, id_type=id_type,
                id_value=source_key,
                description='Auto-created on bulk assign'))
    db.session.commit()
    flash(f'Assigned {updated} charges to client.', 'success')
    return redirect(url_for('charges', unmatched='1'))


# ── Invoices ──────────────────────────────────────────────────────────────────

@app.route('/invoices')
@login_required
def invoices():
    status = request.args.get('status', '')
    period = request.args.get('period', '')
    q = Invoice.query
    if status: q = q.filter_by(status=status)
    if period: q = q.filter_by(billing_period=period)
    invs = q.order_by(Invoice.created_at.desc()).all()
    periods = db.session.query(Invoice.billing_period).distinct().order_by(Invoice.billing_period.desc()).all()
    return render_template('invoices/list.html', invoices=invs,
        periods=[p[0] for p in periods if p[0]],
        selected_period=period, selected_status=status)

@app.route('/invoices/generate', methods=['GET', 'POST'])
@login_required
def invoice_generate():
    s = get_settings()
    if request.method == 'POST':
        period = request.form['billing_period']
        client_ids = request.form.getlist('client_ids')
        if not client_ids:
            # all clients with unmatched charges in period
            from sqlalchemy import distinct
            cids = (db.session.query(distinct(RawCharge.client_id))
                    .join(ImportBatch, RawCharge.batch_id == ImportBatch.id)
                    .filter(ImportBatch.billing_period == period,
                            RawCharge.matched == True,
                            RawCharge.invoiced == False,
                            RawCharge.client_id.isnot(None))
                    .all())
            client_ids = [c[0] for c in cids]

        run = InvoiceRun(billing_period=period, created_by=current_user.id)
        db.session.add(run)
        db.session.flush()

        created = generate_invoices(period, [int(i) for i in client_ids],
                                    db.session, run.id, current_user.id, s)
        db.session.commit()
        flash(f'Generated {len(created)} invoice(s) for {period}.', 'success')
        return redirect(url_for('invoices', period=period))

    # GET — show form
    periods = (db.session.query(ImportBatch.billing_period)
               .distinct().order_by(ImportBatch.billing_period.desc()).all())
    clients = Client.query.filter_by(active=True).order_by(Client.name).all()
    return render_template('invoices/generate.html',
        periods=[p[0] for p in periods if p[0]], clients=clients)

@app.route('/invoices/<int:id>')
@login_required
def invoice_detail(id):
    inv = db.get_or_404(Invoice, id)
    return render_template('invoices/detail.html', invoice=inv, settings=get_settings())

@app.route('/invoices/<int:id>/pdf')
@login_required
def invoice_pdf(id):
    inv = db.get_or_404(Invoice, id)
    s = get_settings()
    pdf_bytes = generate_pdf(inv, s)
    return send_file(
        io.BytesIO(bytes(pdf_bytes)),
        mimetype='application/pdf',
        as_attachment=request.args.get('download') == '1',
        download_name=f"Invoice_{inv.invoice_number}.pdf"
    )

@app.route('/invoices/<int:id>/status', methods=['POST'])
@login_required
def invoice_status(id):
    inv = db.get_or_404(Invoice, id)
    inv.status = request.form['status']
    db.session.commit()
    flash(f'Invoice marked as {inv.status}.', 'success')
    return redirect(url_for('invoice_detail', id=id))

@app.route('/invoices/<int:id>/delete', methods=['POST'])
@login_required
def invoice_delete(id):
    inv = db.get_or_404(Invoice, id)
    if inv.status == 'paid':
        flash('Cannot delete a paid invoice.', 'danger')
        return redirect(url_for('invoice_detail', id=id))
    # Unmark charges
    for line in inv.lines:
        RawCharge.query.filter_by(invoice_line_id=line.id).update(
            {'invoiced': False, 'invoice_line_id': None})
    db.session.delete(inv)
    db.session.commit()
    flash('Invoice deleted.', 'success')
    return redirect(url_for('invoices'))


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    s = get_settings()
    users = User.query.all()
    if request.method == 'POST':
        for field in ('company_name','address_line1','address_line2','city','postcode',
                      'phone','email','website','vat_number','company_number',
                      'bank_name','bank_sort_code','bank_account','invoice_prefix'):
            setattr(s, field, request.form.get(field, ''))
        s.default_markup_pct = float(request.form.get('default_markup_pct', 30))
        s.default_vat_rate = float(request.form.get('default_vat_rate', 20))
        s.payment_terms_days = int(request.form.get('payment_terms_days', 30))
        db.session.commit()
        flash('Settings saved.', 'success')
        return redirect(url_for('settings'))
    return render_template('settings.html', s=s, users=users)

@app.route('/settings/users/new', methods=['POST'])
@login_required
def user_new():
    if current_user.role != 'admin':
        abort(403)
    u = User(username=request.form['username'], email=request.form['email'],
             role=request.form.get('role', 'user'))
    u.set_password(request.form['password'])
    db.session.add(u)
    db.session.commit()
    flash(f'User {u.username} created.', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/users/<int:id>/delete', methods=['POST'])
@login_required
def user_delete(id):
    if current_user.role != 'admin':
        abort(403)
    if current_user.id == id:
        flash('Cannot delete your own account.', 'danger')
        return redirect(url_for('settings'))
    u = db.get_or_404(User, id)
    db.session.delete(u)
    db.session.commit()
    flash('User deleted.', 'success')
    return redirect(url_for('settings'))


# ── API helpers ───────────────────────────────────────────────────────────────

@app.route('/api/unmatched-keys')
@login_required
def api_unmatched_keys():
    """Return unique source_keys that have unmatched charges, for the assign UI."""
    from sqlalchemy import func
    keys = (db.session.query(RawCharge.source_key,
                             func.count(RawCharge.id).label('count'),
                             func.sum(RawCharge.cost_amount).label('total'))
            .filter(RawCharge.matched == False, RawCharge.invoiced == False,
                    RawCharge.source_key.isnot(None))
            .group_by(RawCharge.source_key)
            .order_by(func.count(RawCharge.id).desc())
            .all())
    return jsonify([{'key': k, 'count': c, 'total': round(t or 0, 4)} for k, c, t in keys])


# ── Init ──────────────────────────────────────────────────────────────────────

def create_tables():
    with app.app_context():
        db.create_all()
        # Create default admin if no users exist
        if not User.query.first():
            admin = User(username='admin', email='admin@synthesisit.co.uk', role='admin')
            admin.set_password('changeme123')
            db.session.add(admin)
            # Default company settings
            s = CompanySettings(
                company_name='Synthesis IT',
                invoice_prefix='SIT',
                next_invoice_number=1001,
                default_markup_pct=30.0,
                default_vat_rate=20.0,
                payment_terms_days=30,
            )
            db.session.add(s)
            db.session.commit()
            print('Created default admin user: admin / changeme123')


if __name__ == '__main__':
    create_tables()
    app.run(debug=True, host='0.0.0.0', port=5000)
