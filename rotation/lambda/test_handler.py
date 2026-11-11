"""Unit tests for handler.py. No AWS or Databricks access needed.
Run: python3 -I -m unittest rotation/lambda/test_handler.py  (from the repo root)
     or: cd rotation/lambda && python3 -m unittest test_handler
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handler as h  # noqa: E402

ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:redshift/fed-AbCdEf"
SP_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:databricks/rotation-sp-XyZ123"


class FakeSecrets:
    def __init__(self):
        self.values = {
            ARN: {"username": "redshift_app", "password": "s3cr3t'pw"},
            SP_ARN: {"client_id": "cid", "client_secret": "csecret"},
        }

    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps(self.values[SecretId]), "VersionId": "v-123"}


class FakeDb:
    def __init__(self):
        self.puts, self.sqls = [], []

    def put_secret(self, scope, key, value):
        self.puts.append((scope, key, value))

    def get_connection(self, name):
        return {"options": {"host": "rs.example.redshift.amazonaws.com", "port": "5439"}}

    def sql(self, statement, warehouse_id):
        self.sqls.append(statement)
        return []


MAPPINGS = [{"secret_id": ARN, "scope": "redshift-fed-scope", "user_key": "app_id",
             "password_key": "app_secret", "connection": "redshift_fed",
             "validate_sql": "SELECT 1 FROM redshift_fed.public.t LIMIT 1"}]


def rotation_event(secret_id, name="RotationSucceeded"):
    return {"source": "aws.secretsmanager", "detail-type": "AWS Service Event via CloudTrail",
            "detail": {"eventName": name, "additionalEventData": {"SecretId": secret_id}}}


class HandlerTest(unittest.TestCase):
    def setUp(self):
        os.environ.update({"SECRET_MAPPINGS": json.dumps(MAPPINGS), "DATABRICKS_WAREHOUSE_ID": "wh1",
                           "DATABRICKS_HOST": "dbc.example", "DATABRICKS_SP_SECRET_ID": SP_ARN})
        self.secrets, self.db = FakeSecrets(), FakeDb()

    def run_handler(self, event):
        return h.handler(event, secret_client=self.secrets, db=self.db)

    def test_rotation_syncs_scope_and_alters_connection(self):
        out = self.run_handler(rotation_event(ARN))
        self.assertEqual(len(out["synced"]), 1)
        self.assertEqual(self.db.puts, [("redshift-fed-scope", "app_id", "redshift_app"),
                                        ("redshift-fed-scope", "app_secret", "s3cr3t'pw")])
        alter = self.db.sqls[0]
        self.assertTrue(alter.startswith("ALTER CONNECTION `redshift_fed` OPTIONS ("))
        self.assertIn("host 'rs.example.redshift.amazonaws.com'", alter)
        self.assertIn("port '5439'", alter)
        self.assertIn("password secret('redshift-fed-scope', 'app_secret')", alter)
        self.assertEqual(self.db.sqls[1], MAPPINGS[0]["validate_sql"])

    def test_secret_values_never_in_sql_or_result(self):
        out = self.run_handler(rotation_event(ARN))
        blob = json.dumps(out) + " ".join(self.db.sqls)
        self.assertNotIn("s3cr3t", blob)
        self.assertNotIn("redshift_app", blob)

    def test_put_secret_value_event_with_friendly_name(self):
        ev = {"detail": {"eventName": "PutSecretValue", "requestParameters": {"secretId": "redshift/fed"}}}
        self.assertEqual(len(self.run_handler(ev)["synced"]), 1)

    def test_unrelated_secret_is_ignored(self):
        out = self.run_handler(rotation_event("arn:aws:secretsmanager:us-east-1:1:secret:other-Q1"))
        self.assertEqual(out, {"synced": []})
        self.assertEqual(self.db.puts, [])

    def test_name_prefix_does_not_match(self):
        ev = {"detail": {"eventName": "PutSecretValue", "requestParameters": {"secretId": "redshift/fab"}}}
        self.assertEqual(self.run_handler(ev), {"synced": []})

    def test_unrelated_event_name_is_ignored(self):
        self.assertEqual(self.run_handler(rotation_event(ARN, "GetSecretValue")), {"synced": []})

    def test_sync_all(self):
        self.assertEqual(len(self.run_handler({"sync_all": True})["synced"]), 1)

    def test_existing_credential_options_are_replaced_not_duplicated(self):
        stmt = h.build_alter_connection("c", {"host": "h", "port": "1", "user": "x", "password": "y"},
                                        "s", "u", "p")
        self.assertEqual(stmt.count("user "), 1)
        self.assertNotIn("'x'", stmt)
        self.assertNotIn("'y'", stmt)

    def test_missing_fields_fail(self):
        self.secrets.values[ARN] = {"username": "only"}
        with self.assertRaises(ValueError):
            self.run_handler(rotation_event(ARN))


if __name__ == "__main__":
    unittest.main()
