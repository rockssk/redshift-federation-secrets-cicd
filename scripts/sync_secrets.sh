#!/usr/bin/env bash
# Copy the Redshift credentials from AWS Secrets Manager into the Databricks
# secret scope. Values are masked in the GitHub log and piped through stdin, so
# they never appear in argv, files, Terraform, or logs.
# Prints the secret's VersionId (not sensitive) for Terraform's secret_version.
# Usage: sync_secrets.sh <aws-secret-id> <scope> [user_key] [password_key]
set -euo pipefail

SECRET_ID="$1"; SCOPE="$2"; USER_KEY="${3:-app_id}"; PW_KEY="${4:-app_secret}"

RESP=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --output json)
VERSION=$(jq -r '.VersionId' <<<"$RESP")
# Redshift-managed secrets are JSON: {"username": "...", "password": "...", ...}
U=$(jq -r '.SecretString | fromjson | .username' <<<"$RESP")
P=$(jq -r '.SecretString | fromjson | .password' <<<"$RESP")
unset RESP
[[ -n "$U" && "$U" != null && -n "$P" && "$P" != null ]] || { echo "secret $SECRET_ID lacks username/password" >&2; exit 1; }

if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  echo "::add-mask::$U"
  echo "::add-mask::$P"
fi

printf '%s' "$U" | databricks secrets put-secret "$SCOPE" "$USER_KEY"
printf '%s' "$P" | databricks secrets put-secret "$SCOPE" "$PW_KEY"
unset U P

echo "synced keys $USER_KEY, $PW_KEY into scope $SCOPE (secret version $VERSION)" >&2
echo "$VERSION"
