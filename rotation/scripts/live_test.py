"""Live test of the rotation flow against a real workspace, without AWS.

Answers the open question "does a connection pick up a rotated secret-scope
value on its own, or does it need ALTER CONNECTION?", then runs the real
handler (with AWS Secrets Manager stubbed) to prove the automated fix works.

Uses the public, read-only RNAcentral Postgres as the federated source.
Credentials come from environment variables, never from argv or files.

Usage:
  DATABRICKS_PROFILE=<profile> WAREHOUSE_ID=<warehouse-id> \
  SRC_USER=reader SRC_PASSWORD=... \
  python3 -I rotation/scripts/live_test.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lambda"))
import handler as h  # noqa: E402

PROFILE = os.environ["DATABRICKS_PROFILE"]
WAREHOUSE = os.environ["WAREHOUSE_ID"]
SCOPE = os.environ.get("SCOPE", "rotation-demo-scope")
CONN = os.environ.get("CONNECTION", "rotation_demo_conn")
PROBE = os.environ.get("PROBE_SQL", "SELECT count(*) AS n FROM rotation_demo_federated.rnacen.rnc_database")
GOOD_USER, GOOD_PW = os.environ["SRC_USER"], os.environ["SRC_PASSWORD"]
BAD_PW = "definitely-not-the-password"


class CliAuthDatabricks(h.Databricks):
    """Same REST calls as the Lambda, but authenticated with the local CLI profile."""

    def __init__(self, profile):
        host = json.loads(subprocess.check_output(
            ["databricks", "auth", "describe", "--profile", profile, "-o", "json"]))["details"]["host"]
        super().__init__(host, None, None)
        self._profile = profile

    def _oauth_token(self):
        out = subprocess.check_output(["databricks", "auth", "token", "--profile", self._profile])
        return json.loads(out)["access_token"]


class StubSecrets:
    """Stands in for AWS Secrets Manager: returns whatever value we 'rotated' to."""

    def __init__(self):
        self.value = {"username": GOOD_USER, "password": GOOD_PW}

    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps(self.value), "VersionId": "local-test"}


def probe(db):
    # A unique literal per call changes the query text, so the SQL warehouse's
    # result cache can't answer and every probe really goes to the source.
    import uuid

    sql = PROBE.replace("SELECT ", f"SELECT '{uuid.uuid4().hex}' AS nonce, ", 1)
    try:
        db.sql(sql, WAREHOUSE)
        return "OK"
    except RuntimeError as e:
        return "FAIL: " + str(e)[:160]


def main():
    db = CliAuthDatabricks(PROFILE)
    secrets = StubSecrets()
    mapping = {"secret_id": "arn:aws:secretsmanager:us-east-1:000000000000:secret:demo/rnacentral-AbCdEf",
               "scope": SCOPE, "user_key": "app_id", "password_key": "app_secret",
               "connection": CONN, "validate_sql": PROBE}
    os.environ.update({"SECRET_MAPPINGS": json.dumps([mapping]), "DATABRICKS_WAREHOUSE_ID": WAREHOUSE})
    event = {"detail": {"eventName": "RotationSucceeded",
                        "additionalEventData": {"SecretId": mapping["secret_id"]}}}
    results = {}

    results["1_baseline"] = probe(db)

    # 2. Change ONLY the scope value (what a naive sync job would do), no ALTER.
    db.put_secret(SCOPE, "app_secret", BAD_PW)
    results["2_scope_changed_no_alter"] = probe(db)

    # 3. Same wrong value, now with ALTER CONNECTION re-referencing the secret.
    db.sql(h.build_alter_connection(CONN, db.get_connection(CONN).get("options", {}),
                                    SCOPE, "app_id", "app_secret"), WAREHOUSE)
    results["3_bad_value_after_alter"] = probe(db)

    # 4. "Rotate" back to the good value and let the real handler do the work.
    secrets.value = {"username": GOOD_USER, "password": GOOD_PW}
    try:
        out = h.handler(event, secret_client=secrets, db=db)
        results["4_handler"] = "OK " + json.dumps(out["synced"][0])
    except Exception as e:  # restore below regardless
        results["4_handler"] = "FAIL: " + str(e)[:200]
    results["5_after_handler"] = probe(db)

    # Always leave the demo working.
    if results["5_after_handler"] != "OK":
        db.put_secret(SCOPE, "app_id", GOOD_USER)
        db.put_secret(SCOPE, "app_secret", GOOD_PW)
        db.sql(h.build_alter_connection(CONN, db.get_connection(CONN).get("options", {}),
                                        SCOPE, "app_id", "app_secret"), WAREHOUSE)
        results["6_restored"] = probe(db)

    print(json.dumps(results, indent=2))
    no_alter = results["2_scope_changed_no_alter"]
    print("\nConclusion:", "connection COPIES the secret value; ALTER CONNECTION is required after rotation"
          if no_alter == "OK" and results["3_bad_value_after_alter"] != "OK"
          else "connection resolves the scope at query time (or test inconclusive), see results")


if __name__ == "__main__":
    main()
