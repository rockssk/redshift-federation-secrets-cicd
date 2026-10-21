#!/usr/bin/env bash
# Run one SQL statement on a SQL warehouse via the Statement Execution API.
# Auth comes from the environment (in CI: DATABRICKS_HOST, DATABRICKS_CLIENT_ID,
# DATABRICKS_AUTH_TYPE=github-oidc); DATABRICKS_PROFILE is optional for local use.
# Usage: run_sql.sh "<sql>"   (env: WAREHOUSE_ID)
set -euo pipefail

SQL="$1"
: "${WAREHOUSE_ID:?set WAREHOUSE_ID}"
PROFILE_ARGS=()
[[ -n "${DATABRICKS_PROFILE:-}" ]] && PROFILE_ARGS=(--profile "$DATABRICKS_PROFILE")

RESP=$(databricks api post /api/2.0/sql/statements/ "${PROFILE_ARGS[@]}" --json \
  "$(jq -n --arg s "$SQL" --arg wh "$WAREHOUSE_ID" \
     '{statement: $s, warehouse_id: $wh, wait_timeout: "50s", on_wait_timeout: "CANCEL"}')")

STATE=$(jq -r '.status.state' <<<"$RESP")
if [[ "$STATE" != "SUCCEEDED" ]]; then
  echo "SQL failed ($STATE): $(jq -r '.status.error.message // .' <<<"$RESP")" >&2
  exit 1
fi
jq -c '.result.data_array // []' <<<"$RESP"
