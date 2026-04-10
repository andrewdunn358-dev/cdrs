# Synthesis IT Billing System

A self-hosted telecoms billing platform for Gamma and Nasstar services.

## Running with Docker (recommended)

### Install Docker on Debian
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
sudo apt-get install -y docker-compose-plugin
```

### Start the app
```bash
# 1. Copy the billing folder to your machine

# 2. Set a secret key
cp .env.example .env
# Generate one: python3 -c "import secrets; print(secrets.token_hex(32))"
# Paste it into .env as SECRET_KEY

# 3. Build and start
docker compose up -d

# 4. Open http://localhost:5000
# Login: admin / changeme123  (change immediately in Settings)
```

### Useful commands
```bash
docker compose logs -f                   # watch logs
docker compose down                      # stop
docker compose up -d --build             # rebuild after changes

# Backup database
docker compose cp billing:/data/billing.db ./billing-$(date +%Y%m%d).db
```

## Monthly Billing Workflow

1. Download Gamma files from portal (FF files + _V3.txt call CDRs)
2. Download Nasstar .CDR files
3. **Import → Upload Files** — drop all files at once, formats auto-detected
4. **Charges → Unmatched** — first month: assign unmatched source keys to clients, tick "Save identifier" so future months auto-match
5. **Invoices → Generate** — select period, generates draft invoices with 30% markup
6. Review, download PDF, mark Sent / Paid

## Client Identifier Types

| Type | Example | Matches |
|---|---|---|
| nasstar_account | 0003057003 | Nasstar CDR files |
| gamma_ipdc_endpoint | DC2N20BCT78556 | IPDC charges + SIP calls |
| gamma_cli | 01912574578 | DIV, NTS, FTC, IBRS calls |
| gamma_circuit | 279056 | Broadband rentals |
| gamma_ces_circuit | CES00023767-01 | Leased line rentals |
| gamma_wlr | 1912571571 | WLR line rentals |
| gamma_inbound | 2071181168 | Inbound number rentals |

## Environment Variables

| Variable | Description |
|---|---|
| SECRET_KEY | Flask session secret — always set this in production |
| DATABASE_URL | Defaults to sqlite:////data/billing.db in Docker |
