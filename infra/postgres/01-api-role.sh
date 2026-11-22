#!/bin/sh
# Runs once on an empty cluster via the official PostgreSQL entrypoint.
export AGENTOS_INIT_PASSWORD="$(cat /secrets/api/password)"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv api_password AGENTOS_INIT_PASSWORD
CREATE ROLE agentos_api LOGIN PASSWORD :'api_password';
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
unset AGENTOS_INIT_PASSWORD
