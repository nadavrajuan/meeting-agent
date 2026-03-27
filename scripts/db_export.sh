#!/bin/bash
# Export local PostgreSQL data and import to production
# Usage: bash scripts/db_export.sh
#
# Requires:
#   - Local docker compose running (docker compose up -d db)
#   - SSH access to production EC2
#   - SSH_HOST and SSH_USER env vars set, or edit below

SSH_HOST="${SSH_HOST:-52.57.122.123}"
SSH_USER="${SSH_USER:-ubuntu}"
DEPLOY_DIR="/opt/meeting-agent"
DUMP_FILE="/tmp/meeting_agent_$(date +%Y%m%d_%H%M%S).sql"

echo "==> Dumping local database..."
docker compose exec -T db pg_dump -U agent meeting_agent > "$DUMP_FILE"
echo "    Saved to $DUMP_FILE"

echo "==> Copying dump to production..."
scp "$DUMP_FILE" "${SSH_USER}@${SSH_HOST}:/tmp/meeting_agent_dump.sql"

echo "==> Restoring on production..."
ssh "${SSH_USER}@${SSH_HOST}" "
  cd ${DEPLOY_DIR} && \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db \
    psql -U agent -d meeting_agent < /tmp/meeting_agent_dump.sql && \
  rm /tmp/meeting_agent_dump.sql
"

echo "==> Done. Local dump kept at $DUMP_FILE"
