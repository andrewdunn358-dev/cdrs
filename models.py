from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date

db = SQLAlchemy()

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='user')  # admin / user
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw): self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)

class Client(db.Model):
    __tablename__ = 'clients'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    address_line1 = db.Column(db.String(200))
    address_line2 = db.Column(db.String(200))
    city = db.Column(db.String(100))
    postcode = db.Column(db.String(20))
    contact_name = db.Column(db.String(150))
    contact_email = db.Column(db.String(150))
    contact_phone = db.Column(db.String(50))
    account_ref = db.Column(db.String(50), unique=True)
    vat_number = db.Column(db.String(50))
    markup_pct = db.Column(db.Float, default=30.0)
    billing_day = db.Column(db.Integer, default=1)
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    identifiers = db.relationship('ClientIdentifier', backref='client', lazy=True, cascade='all, delete-orphan')
    invoices = db.relationship('Invoice', backref='client', lazy=True)

    def full_address(self):
        parts = [self.address_line1, self.address_line2, self.city, self.postcode]
        return ', '.join(p for p in parts if p)

class ClientIdentifier(db.Model):
    """Maps supplier IDs/CLIs/circuit refs to a client."""
    __tablename__ = 'client_identifiers'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    id_type = db.Column(db.String(50), nullable=False)
    # Types: nasstar_account | gamma_ipdc_endpoint | gamma_cli | gamma_circuit | gamma_ces_circuit | gamma_wlr | gamma_inbound
    id_value = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(200))
    active = db.Column(db.Boolean, default=True)

    __table_args__ = (db.UniqueConstraint('id_type', 'id_value', name='uq_id_type_value'),)

class ImportBatch(db.Model):
    __tablename__ = 'import_batches'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(300), nullable=False)
    file_type = db.Column(db.String(50))  # gamma_bb | gamma_ces | gamma_ipdc | gamma_wlr | gamma_calls | nasstar_cdr | gamma_inbound
    billing_period = db.Column(db.String(20))  # e.g. "2026-02"
    imported_at = db.Column(db.DateTime, default=datetime.utcnow)
    imported_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    record_count = db.Column(db.Integer, default=0)
    matched_count = db.Column(db.Integer, default=0)
    charges = db.relationship('RawCharge', backref='batch', lazy=True)

class RawCharge(db.Model):
    __tablename__ = 'raw_charges'
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey('import_batches.id'), nullable=False)
    source_key = db.Column(db.String(200))   # circuit/endpoint/CLI used for matching
    product_name = db.Column(db.String(300))
    charge_type = db.Column(db.String(50))   # Rental | Connection | Call | Credit
    billing_period = db.Column(db.String(20))
    call_date = db.Column(db.Date)
    call_duration = db.Column(db.Integer)    # seconds
    destination = db.Column(db.String(100))
    description = db.Column(db.String(300))
    cost_amount = db.Column(db.Float, default=0.0)
    credit_amount = db.Column(db.Float, default=0.0)
    quantity = db.Column(db.Integer, default=1)
    site_name = db.Column(db.String(200))
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=True)
    matched = db.Column(db.Boolean, default=False)
    invoiced = db.Column(db.Boolean, default=False)
    archived = db.Column(db.Boolean, default=False)  # retired by newer import of same type
    invoice_line_id = db.Column(db.Integer, db.ForeignKey('invoice_lines.id'), nullable=True)
    raw_json = db.Column(db.Text)

    client = db.relationship('Client', foreign_keys=[client_id])

class InvoiceRun(db.Model):
    __tablename__ = 'invoice_runs'
    id = db.Column(db.Integer, primary_key=True)
    billing_period = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    status = db.Column(db.String(20), default='draft')  # draft | final
    invoices = db.relationship('Invoice', backref='run', lazy=True)

class Invoice(db.Model):
    __tablename__ = 'invoices'
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('invoice_runs.id'), nullable=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    invoice_number = db.Column(db.String(50), unique=True)
    invoice_date = db.Column(db.Date, default=date.today)
    due_date = db.Column(db.Date)
    billing_period = db.Column(db.String(20))
    subtotal = db.Column(db.Float, default=0.0)
    vat_amount = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default='draft')  # draft | sent | paid
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    lines = db.relationship('InvoiceLine', backref='invoice', lazy=True, cascade='all, delete-orphan')

class InvoiceLine(db.Model):
    __tablename__ = 'invoice_lines'
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoices.id'), nullable=False)
    category = db.Column(db.String(100))   # Calls | Broadband | SIP Trunks | Leased Lines | WLR | Other
    description = db.Column(db.String(300), nullable=False)
    quantity = db.Column(db.Float, default=1.0)
    unit_cost = db.Column(db.Float, default=0.0)
    unit_price = db.Column(db.Float, default=0.0)
    line_total = db.Column(db.Float, default=0.0)
    vat_rate = db.Column(db.Float, default=20.0)
    sort_order = db.Column(db.Integer, default=0)

class CompanySettings(db.Model):
    __tablename__ = 'company_settings'
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(200), default='Synthesis IT')
    address_line1 = db.Column(db.String(200))
    address_line2 = db.Column(db.String(200))
    city = db.Column(db.String(100))
    postcode = db.Column(db.String(20))
    phone = db.Column(db.String(50))
    email = db.Column(db.String(150))
    website = db.Column(db.String(200))
    vat_number = db.Column(db.String(50))
    company_number = db.Column(db.String(50))
    bank_name = db.Column(db.String(100))
    bank_sort_code = db.Column(db.String(20))
    bank_account = db.Column(db.String(30))
    invoice_prefix = db.Column(db.String(10), default='INV')
    next_invoice_number = db.Column(db.Integer, default=1001)
    default_markup_pct = db.Column(db.Float, default=30.0)
    default_vat_rate = db.Column(db.Float, default=20.0)
    payment_terms_days = db.Column(db.Integer, default=30)

class IgnoredKey(db.Model):
    """Source keys that should never appear in unmatched charges."""
    __tablename__ = 'ignored_keys'
    id = db.Column(db.Integer, primary_key=True)
    source_key = db.Column(db.String(200), unique=True, nullable=False)
    reason = db.Column(db.String(200))
    ignored_at = db.Column(db.DateTime, default=datetime.utcnow)
    ignored_by = db.Column(db.Integer, db.ForeignKey('users.id'))
