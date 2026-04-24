#!/bin/bash

# VPS Deployment Script — Sports Data Pipeline + Stats API
# Called by GitHub Actions on every push to main.
#
# What this script does:
#   1. Pulls the latest code (clones on first run)
#   2. Creates / updates a Python virtual environment
#   3. Installs / upgrades dependencies from requirements.txt
#   4. Writes or updates the stats_api systemd unit
#   5. Writes or updates the daily-ingest systemd timer
#   6. Reloads systemd and restarts the stats_api service
#   7. Verifies the service is running

set -e

echo "=========================================="
echo " Sports Data Pipeline — Deployment Script"
echo "=========================================="
echo ""

# ── Config (all overridable via environment variables) ─────────────────────
SERVICE_NAME="${SERVICE_NAME:-sports-stats-api}"
SERVICE_DIR="${SERVICE_DIR:-/home/ubuntu/services/${SERVICE_NAME}}"
VENV_DIR="$SERVICE_DIR/venv"
REPO_URL="${REPO_URL:-https://github.com/joypciu/sports_data_real_time_fetch.git}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
API_PORT="${API_PORT:-8001}"
EXPECTED_REPO_SLUG="${EXPECTED_REPO_SLUG:-joypciu/sports_data_real_time_fetch}"
LOCK_FILE="/tmp/${SERVICE_NAME}.deploy.lock"

# Daily ingest timer name (may differ from the API service name)
INGEST_TIMER_NAME="${SERVICE_NAME}-ingest"

# ── Colour helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
print_success() { echo -e "${GREEN}✓ $1${NC}"; }
print_error()   { echo -e "${RED}✗ $1${NC}"; }
print_info()    { echo -e "${YELLOW}→ $1${NC}"; }

# ── Lock against parallel deploys ───────────────────────────────────────────
if [ -f "$LOCK_FILE" ]; then
    print_error "Another deployment is running for $SERVICE_NAME ($LOCK_FILE exists). Aborting."
    exit 1
fi
trap 'rm -f "$LOCK_FILE"' EXIT
touch "$LOCK_FILE"

# ── User check ──────────────────────────────────────────────────────────────
if [ "$USER" != "ubuntu" ]; then
    print_error "Script must run as user 'ubuntu' (got: $USER)"
    exit 1
fi

print_info "Deploy config:"
echo "  SERVICE_NAME=$SERVICE_NAME"
echo "  SERVICE_DIR=$SERVICE_DIR"
echo "  DEPLOY_BRANCH=$DEPLOY_BRANCH"
echo "  API_PORT=$API_PORT"
echo "  REPO_URL=$REPO_URL"

# ── Code checkout ───────────────────────────────────────────────────────────
if [ ! -d "$SERVICE_DIR" ]; then
    print_info "Creating service directory..."
    sudo mkdir -p "$SERVICE_DIR"
    sudo chown -R ubuntu:ubuntu "$SERVICE_DIR"
fi
cd "$SERVICE_DIR"

if [ ! -d ".git" ]; then
    print_info "First-time setup — cloning repository..."
    git clone "$REPO_URL" .
    print_success "Repository cloned"
else
    print_info "Updating repository..."
    CURRENT_REMOTE="$(git remote get-url origin 2>/dev/null || true)"
    if [ -n "$CURRENT_REMOTE" ] && [[ "$CURRENT_REMOTE" != *"$EXPECTED_REPO_SLUG"* ]]; then
        print_error "Repository mismatch in $SERVICE_DIR"
        echo "  expected: $EXPECTED_REPO_SLUG"
        echo "  actual:   $CURRENT_REMOTE"
        exit 1
    fi
    git fetch origin "$DEPLOY_BRANCH"
    git checkout -f "$DEPLOY_BRANCH"
    git reset --hard "origin/$DEPLOY_BRANCH"
    print_success "Repository updated"
fi

# ── Python virtual environment ──────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    print_info "Creating Python virtual environment..."
    python3 -m venv venv
    print_success "Virtual environment created"
fi

print_info "Installing / upgrading dependencies..."
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt --upgrade -q
print_success "Dependencies installed"

# ── .env file ───────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        print_info ".env created from .env.example — edit it with your secrets before next restart"
    fi
fi

# ── Ensure required runtime directories exist ────────────────────────────────
print_info "Ensuring runtime directories exist..."
mkdir -p "$SERVICE_DIR/db" \
         "$SERVICE_DIR/historical_data" \
         "$SERVICE_DIR/live" \
         "$SERVICE_DIR/archive"
print_success "Runtime directories ready"

# ── Bootstrap historical data (first deploy or missing DB) ───────────────────
DB_FILE="$SERVICE_DIR/db/sports.db"
DATA_DIR="$SERVICE_DIR/historical_data"

if [ ! -f "$DB_FILE" ] || [ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
    print_info "No database or historical data found — running 30-day backfill (this may take a few minutes)..."
    source "$VENV_DIR/bin/activate"

    python daily_ingest.py --days 30 \
        && print_success "Historical ingest complete" \
        || print_info "Ingest finished with some errors — continuing"

    python build_db.py \
        && print_success "Database built from historical data" \
        || print_info "build_db finished with warnings — continuing"
else
    print_info "Existing database found — skipping backfill (daily timer handles ongoing ingestion)"
fi

# ── Systemd unit: stats_api ─────────────────────────────────────────────────
print_info "Writing systemd unit: ${SERVICE_NAME}.service"
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Sports Stats API (realtime_data_fetch)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=${SERVICE_DIR}
EnvironmentFile=${SERVICE_DIR}/.env
ExecStart=${VENV_DIR}/bin/python main.py --port ${API_PORT} --output-dir live
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
print_success "stats_api unit written"

# ── Systemd unit + timer: daily_ingest ─────────────────────────────────────
# The ingest service runs daily_ingest.py then build_db.py sequentially.
# update_db.py (Thread 3 in main.py) handles incremental DuckDB updates every
# 5 minutes during the day; the nightly build_db ensures a clean full rebuild.
print_info "Writing systemd unit + timer: ${INGEST_TIMER_NAME}"
sudo tee "/etc/systemd/system/${INGEST_TIMER_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Daily ESPN data ingest + DB rebuild (realtime_data_fetch)
After=network.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=${SERVICE_DIR}
EnvironmentFile=${SERVICE_DIR}/.env
ExecStart=${VENV_DIR}/bin/python daily_ingest.py
ExecStartPost=${VENV_DIR}/bin/python build_db.py
StandardOutput=journal
StandardError=journal
TimeoutStartSec=1800
EOF

sudo tee "/etc/systemd/system/${INGEST_TIMER_NAME}.timer" > /dev/null <<EOF
[Unit]
Description=Run ESPN daily ingest + DB rebuild every day at 06:00 UTC

[Timer]
OnCalendar=*-*-* 06:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF
print_success "Daily ingest unit + timer written"

# ── Reload systemd and restart services ────────────────────────────────────
print_info "Reloading systemd daemon..."
sudo systemctl daemon-reload

print_info "Enabling and restarting ${SERVICE_NAME}..."
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

print_info "Enabling ${INGEST_TIMER_NAME} timer..."
sudo systemctl enable "${INGEST_TIMER_NAME}.timer"
sudo systemctl start  "${INGEST_TIMER_NAME}.timer"

# ── Verify stats_api is running ────────────────────────────────────────────
sleep 3
if sudo systemctl is-active --quiet "${SERVICE_NAME}"; then
    print_success "${SERVICE_NAME} is running on port ${API_PORT}"
else
    print_error "${SERVICE_NAME} failed to start"
    sudo journalctl -u "${SERVICE_NAME}" -n 30 --no-pager
    exit 1
fi

echo ""
echo "=========================================="
print_success "Deployment complete"
echo "  Stats API: http://localhost:${API_PORT}/health"
echo "  Logs:      sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Timer:     sudo systemctl list-timers ${INGEST_TIMER_NAME}.timer"
echo "=========================================="
