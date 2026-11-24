#!/bin/sh
set -eu
export PGPASSWORD="$(cat /secrets/admin/password)"
export WORKER_PASSWORD="$(cat /secrets/worker/password)"
export PUBLISHER_PASSWORD="$(cat /secrets/publisher/password)"
psql -X -v ON_ERROR_STOP=1 -h postgres -U postgres -d agentos <<'SQL'
\getenv worker_password WORKER_PASSWORD
\getenv publisher_password PUBLISHER_PASSWORD
SELECT 'CREATE ROLE agentos_worker LOGIN' WHERE NOT EXISTS
    (SELECT FROM pg_roles WHERE rolname='agentos_worker') \gexec
SELECT 'CREATE ROLE agentos_publisher LOGIN' WHERE NOT EXISTS
    (SELECT FROM pg_roles WHERE rolname='agentos_publisher') \gexec
ALTER ROLE agentos_worker PASSWORD :'worker_password';
ALTER ROLE agentos_publisher PASSWORD :'publisher_password';
SQL
