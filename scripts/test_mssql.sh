#!/usr/bin/env bash
# SQL Server integration tests (Phase 12, D-040) against a local, throwaway SQL Server 2022
# container. Needs Docker only; no corporate network or credentials.
#   scripts/test_mssql.sh            # start SQL Server if needed, build the test image, run
#   scripts/test_mssql.sh --all      # also run the whole unit-test suite on SQL Server
#   scripts/test_mssql.sh --stop     # remove the SQL Server container afterwards
set -euo pipefail
cd "$(dirname "$0")/.."

NET=uw-test-net
DB=uw-mssql
SA_PASSWORD="Local-Test-Only-1"   # throwaway local container only
IMAGE=uw-pipeline-test

if [[ "${1:-}" == "--stop" ]]; then
  docker rm -f "$DB" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  exit 0
fi

docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
if ! docker inspect "$DB" >/dev/null 2>&1; then
  # SQL Server is amd64-only; Docker Desktop on Apple silicon runs it emulated.
  docker run -d --name "$DB" --platform linux/amd64 --network "$NET" \
    -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD="$SA_PASSWORD" -e MSSQL_PID=Developer \
    mcr.microsoft.com/mssql/server:2022-latest >/dev/null
fi
docker network inspect "$NET" | grep -q "\"Name\": \"$DB\"" || docker network connect "$NET" "$DB"
docker start "$DB" >/dev/null
echo "waiting for SQL Server..."
until docker logs "$DB" 2>&1 | grep -q "ready for client connections"; do sleep 3; done

docker build -q --target test -t "$IMAGE" . >/dev/null
TESTS="-m mssql tests/test_mssql.py"
ALL=""
if [[ "${1:-}" == "--all" ]]; then TESTS="tests"; ALL=1; fi
URL="mssql+pyodbc://sa:${SA_PASSWORD}@${DB}:1433/uwtest?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no"
docker run --rm --network "$NET" -v "$PWD/tests:/src/tests:ro" -v "$PWD/config:/src/config:ro" \
  -e MSSQL_TEST_URL="$URL" -e MSSQL_SA_PASSWORD="$SA_PASSWORD" -e UW_TEST_ON_MSSQL="$ALL" \
  -e TESTS="$TESTS" "$IMAGE" \
  sh -c 'python - <<PY
import os, pyodbc
c = pyodbc.connect("DRIVER={ODBC Driver 18 for SQL Server};SERVER=uw-mssql;UID=sa;PWD="
                   + os.environ["MSSQL_SA_PASSWORD"] + ";Encrypt=no", autocommit=True)
c.execute("IF DB_ID(N\x27uwtest\x27) IS NULL CREATE DATABASE uwtest")
PY
cd /src && python -m pytest -q $TESTS -p no:cacheprovider'
