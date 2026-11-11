"""Sync rotated AWS Secrets Manager credentials into a Databricks secret scope and
refresh the Unity Catalog federation connection that uses them.

Trigger: an EventBridge rule on Secrets Manager events (RotationSucceeded,
PutSecretValue, UpdateSecret) for the configured secret(s).

For each mapped secret the handler:
  1. reads the new value from Secrets Manager (JSON with username/password),
  2. writes both values into the Databricks secret scope (Secrets API),
  3. re-runs ALTER CONNECTION ... OPTIONS (..., user secret(), password secret()),
     because a connection copies the secret values when it is created or altered,
  4. optionally runs a validation query through the connection.

Credential values never appear in logs, in the SQL text, or in the return value.
Only the standard library and boto3 (in the Lambda runtime) are used.

Configuration (environment variables):
  DATABRICKS_HOST            https://<workspace>.cloud.databricks.com
  DATABRICKS_SP_SECRET_ID    Secrets Manager id holding {"client_id": "...", "client_secret": "..."}
                             for the automation service principal (OAuth M2M)
  DATABRICKS_WAREHOUSE_ID    SQL warehouse used for ALTER CONNECTION / validation
  SECRET_MAPPINGS            JSON list, one entry per AWS secret:
    [{"secret_id": "arn:aws:secretsmanager:...:secret:redshift/fed-AbCdEf",
      "scope": "redshift-fed-scope", "user_key": "app_id", "password_key": "app_secret",
      "connection": "redshift_fed",
      "validate_sql": "SELECT 1 FROM redshift_fed.public.some_table LIMIT 1"}]
"""
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger()
log.setLevel(logging.INFO)

# Options that carry credentials. They are always re-sent as secret() references.
CREDENTIAL_OPTIONS = {"user", "password"}
SECRET_EVENTS = {"RotationSucceeded", "PutSecretValue", "UpdateSecret"}


# ── Secret source (AWS Secrets Manager by default; injectable for local tests) ──
def _boto_client():
    import boto3  # available in the Lambda runtime

    return boto3.client("secretsmanager")


def get_secret_json(secret_id, client=None):
    client = client or _boto_client()
    resp = client.get_secret_value(SecretId=secret_id)
    return json.loads(resp["SecretString"]), resp.get("VersionId", "")


# ── Databricks REST helpers ──────────────────────────────────────────────────
class Databricks:
    def __init__(self, host, client_id, client_secret):
        self.host = host if host.startswith("http") else "https://" + host
        self._client_id, self._client_secret = client_id, client_secret
        self._token, self._expiry = None, 0

    def _oauth_token(self):
        if self._token and time.time() < self._expiry - 60:
            return self._token
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "all-apis"}).encode()
        req = urllib.request.Request(f"{self.host}/oidc/v1/token", data=body, method="POST")
        import base64

        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        req.add_header("Authorization", f"Basic {basic}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        self._token, self._expiry = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._token

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f"{self.host}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._oauth_token()}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            # The error body never contains the secret values we sent.
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {e.read()[:500]!r}") from None

    def put_secret(self, scope, key, value):
        self.call("POST", "/api/2.0/secrets/put", {"scope": scope, "key": key, "string_value": value})

    def get_connection(self, name):
        return self.call("GET", f"/api/2.1/unity-catalog/connections/{urllib.parse.quote(name)}")

    def sql(self, statement, warehouse_id):
        resp = self.call("POST", "/api/2.0/sql/statements/", {
            "statement": statement, "warehouse_id": warehouse_id,
            "wait_timeout": "50s", "on_wait_timeout": "CANCEL"})
        state = resp.get("status", {}).get("state")
        if state != "SUCCEEDED":
            msg = resp.get("status", {}).get("error", {}).get("message", "")
            raise RuntimeError(f"SQL {state}: {msg[:500]}")
        return resp.get("result", {}).get("data_array", [])


# ── Core logic ───────────────────────────────────────────────────────────────
def _sql_str(v):
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_alter_connection(name, current_options, scope, user_key, password_key):
    """ALTER CONNECTION replaces the whole option list, so carry over the
    non-credential options (host, port, ...) and re-reference the secrets."""
    keep = {k: v for k, v in sorted(current_options.items()) if k not in CREDENTIAL_OPTIONS}
    parts = [f"{k} {_sql_str(v)}" for k, v in keep.items()]
    parts.append(f"user secret({_sql_str(scope)}, {_sql_str(user_key)})")
    parts.append(f"password secret({_sql_str(scope)}, {_sql_str(password_key)})")
    return f"ALTER CONNECTION `{name}` OPTIONS ({', '.join(parts)})"


def sync_one(db, mapping, warehouse_id, secret_client=None):
    creds, version = get_secret_json(mapping["secret_id"], secret_client)
    user, password = creds.get("username"), creds.get("password")
    if not user or not password:
        raise ValueError(f"secret {mapping['secret_id']} has no username/password fields")

    scope = mapping["scope"]
    user_key = mapping.get("user_key", "app_id")
    pw_key = mapping.get("password_key", "app_secret")
    db.put_secret(scope, user_key, user)
    db.put_secret(scope, pw_key, password)
    del user, password, creds

    conn = mapping["connection"]
    options = db.get_connection(conn).get("options", {})
    db.sql(build_alter_connection(conn, options, scope, user_key, pw_key), warehouse_id)

    validated = None
    if mapping.get("validate_sql"):
        db.sql(mapping["validate_sql"], warehouse_id)
        validated = True

    result = {"secret_id": mapping["secret_id"], "secret_version": version, "scope": scope,
              "connection": conn, "validated": validated}
    log.info("synced %s", json.dumps(result))
    return result


def _event_secret_id(event):
    detail = event.get("detail", {}) if isinstance(event, dict) else {}
    if detail.get("eventName") and detail["eventName"] not in SECRET_EVENTS:
        return None
    params = detail.get("requestParameters") or {}
    add = detail.get("additionalEventData") or {}
    return params.get("secretId") or add.get("SecretId") or event.get("secret_id")


def _name_of(secret_id):
    """Friendly name of a secret id. ARNs end with '-' plus a 6-character random suffix."""
    if ":secret:" not in secret_id:
        return secret_id
    tail = secret_id.split(":secret:")[-1]
    return tail[:-7] if len(tail) > 7 and tail[-7] == "-" else tail


def _matches(mapping, secret_id):
    # Events may carry the full ARN or the friendly name.
    return secret_id == mapping["secret_id"] or _name_of(secret_id) == _name_of(mapping["secret_id"])


def handler(event, context=None, secret_client=None, db=None):
    mappings = json.loads(os.environ["SECRET_MAPPINGS"])
    warehouse_id = os.environ["DATABRICKS_WAREHOUSE_ID"]

    secret_id = _event_secret_id(event)
    if event.get("sync_all"):
        targets = mappings
    elif secret_id:
        targets = [m for m in mappings if _matches(m, secret_id)]
    else:
        targets = []
    if not targets:
        log.info("no mapping for event secret %s; nothing to do", secret_id)
        return {"synced": []}

    if db is None:
        sp, _ = get_secret_json(os.environ["DATABRICKS_SP_SECRET_ID"], secret_client)
        db = Databricks(os.environ["DATABRICKS_HOST"], sp["client_id"], sp["client_secret"])

    return {"synced": [sync_one(db, m, warehouse_id, secret_client) for m in targets]}
