# Automated rotation: AWS Secrets Manager → Databricks secret scope → UC connection

Removes the manual step where an AWS admin copies a secret from AWS Secrets
Manager into a Databricks secret scope. Rotation in AWS triggers the sync and
refreshes the Lakehouse Federation connection. Nobody copies a credential.

```
AWS Secrets Manager ──RotationSucceeded / PutSecretValue / UpdateSecret──► EventBridge
                                                                              │
                                                                              ▼
                                            Lambda  (IAM: GetSecretValue on mapped ARNs only)
                                              │  OAuth M2M as the automation service principal
                                              ├─ 1. secrets/put  → scope keys app_id, app_secret
                                              ├─ 2. ALTER CONNECTION … OPTIONS (host, port,
                                              │        user secret('scope','app_id'),
                                              │        password secret('scope','app_secret'))
                                              └─ 3. optional validation query through the connection
                     failures → 2 retries → SQS dead-letter queue + CloudWatch alarm
```

## Why step 2 (ALTER CONNECTION) is needed (verified)
A UC connection **takes the secret's value when the connection is created or
altered**. Updating only the secret scope does **not** change a connection that
already exists. Verified on 2026-11-10 with `scripts/live_test.py` (two runs;
each probe query is unique, so the warehouse result cache can't mask the result):

| Step | Result |
|---|---|
| 1. Baseline query through the connection | OK |
| 2. Wrong password written to the scope, **no ALTER** | **Still OK**: the connection kept the old value |
| 3. Same wrong value, then `ALTER CONNECTION` | **Fails** (`FAILED_JDBC.CONNECTION`): ALTER re-read the scope |
| 4. Correct value "rotated" through the real handler (scope write + ALTER + validation) | OK |
| 5. Query after the handler | OK |

So the sync always re-runs `ALTER CONNECTION`. `ALTER CONNECTION … OPTIONS`
replaces the whole option list, so the Lambda reads the connection's current
non-credential options (host, port, …) and sends them back unchanged. The
test confirmed host and port were preserved.

## What's here
| Path | Purpose |
|---|---|
| `lambda/handler.py` | The sync. Standard library + boto3 only; no secret values in logs, SQL text, or return value |
| `lambda/test_handler.py` | Unit tests (no AWS/Databricks needed): `cd rotation/lambda && python3 -m unittest test_handler` |
| `terraform/` | AWS stack: Lambda, least-privilege IAM role, EventBridge rule, retries, DLQ, error alarm. Terraform only references secret **ARNs**, never their values |
| `terraform/databricks.tf.example` | Databricks side: automation SP, scope `WRITE` ACL, warehouse `CAN USE`, connection ownership |
| `scripts/live_test.py` | Runs the real handler against a workspace with AWS stubbed, and tests whether ALTER is required |

## Setup
1. **Automation service principal** (Databricks), with:
   - `WRITE` on the secret scope (includes READ, which `secret()` needs when the DDL runs)
   - `CAN USE` on one SQL warehouse
   - ownership of the connection (`ALTER CONNECTION <c> SET OWNER TO \`<sp-app-id>\``) or `MANAGE` on it
2. **Store the SP's OAuth secret in AWS Secrets Manager** as
   `{"client_id": "...", "client_secret": "..."}`. Pipe it from the API straight
   into Secrets Manager (see `databricks.tf.example`) so it never touches disk or Terraform.
3. **Deploy the AWS stack**:
   ```bash
   cd rotation/terraform
   cp example.tfvars prod.tfvars   # fill in ARNs, host, warehouse, scope, connection
   terraform init && terraform apply -var-file=prod.tfvars
   ```
4. **Seed once** (or after onboarding a new secret):
   `aws lambda invoke --function-name databricks-secret-sync --payload '{"sync_all": true}' --cli-binary-format raw-in-base64-out /dev/stdout`
5. **Rotate in AWS as usual.** The scope and connection follow within seconds.

## Security properties
- The Lambda role can read **only** the mapped secrets plus the SP secret (`GetSecretValue` per ARN, optional `kms:Decrypt`).
- Values move only through memory: Secrets Manager → TLS → Databricks Secrets API. They are not in env vars, logs, SQL text, or Terraform state.
- The SP has no data privileges. It can write one scope and alter one connection.
- Audit on both sides: CloudTrail (`GetSecretValue`), Databricks `system.access.audit` (`putSecret`, connection updates).
- Network: if the workspace has IP access lists or PrivateLink only, run the Lambda in a VPC (`subnet_ids`) with a route to the workspace and to Secrets Manager.

## Design choices
- **Event-driven rather than scheduled**: the new value is in Databricks within seconds of rotation, so nothing runs with a stale value. For extra safety, add an EventBridge schedule that invokes `{"sync_all": true}` daily.
- **Why not Terraform for values**: `aws_secretsmanager_secret_version` data sources and `databricks_secret.string_value` both write the value into state.
- **Future simplification**: Unity Catalog now supports secrets backed by AWS Secrets Manager (Beta). There's no copy step and rotation needs no Databricks change. Today they work in `dbutils.secrets` and UC UDFs but not in `secret()` or connection DDL. When connections support them, this pipeline can be retired.
