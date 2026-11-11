# Redshift Federation via Git CI/CD: credentials never in Terraform

**Audience:** Data platform and security teams  **Scope:** Unity Catalog foreign catalog on Amazon Redshift, deployed from GitHub

## The problem
The connection was created with `user = "{{secrets/redshift-fed-scope/app_id}}"`. That placeholder is expanded **only** in
cluster Spark config, environment variables, and job parameters. A Unity Catalog connection stores option values **literally**,
so Redshift received the placeholder text as the username. The Terraform `databricks_connection` resource has no way to
reference a secret ([provider issue #4327](https://github.com/databricks/terraform-provider-databricks/issues/4327), still open). The only
Terraform-native workaround is to pass real values, which puts the password in plans and state. **We don't recommend that.**

## The fix
Unity Catalog connection DDL accepts the **`secret()` function**, and the docs recommend it for credential options
([CREATE](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-create-connection) /
[ALTER CONNECTION](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-connection)):

```sql
CREATE CONNECTION redshift_fed TYPE REDSHIFT OPTIONS (
  host '<cluster>.redshift.amazonaws.com', port '5439',
  user     secret('redshift-fed-scope', 'app_id'),
  password secret('redshift-fed-scope', 'app_secret'));
```

Terraform runs this SQL through the **SQL Statement Execution API**. Everything else stays native Terraform.

## Who does what
| # | Step | Tool | What Terraform sees |
|---|---|---|---|
| 1 | Secret scope + ACLs | **Terraform** `databricks_secret_scope`, `databricks_secret_acl` | Scope name |
| 2 | Secret values | **API/CLI**: CI copies them from AWS Secrets Manager into the scope (masked, via stdin) | Nothing, only the secret *VersionId* |
| 3 | Connection | **Terraform** `terraform_data` → SQL API: `CREATE` once, `ALTER` on change or rotation, `DROP` on destroy | Scope and key **names** |
| 4 | Foreign catalog + grants | **Terraform** `databricks_catalog` (`connection_name`), `databricks_grants` | Catalog, groups |

DABs aren't required for this. If a team standardises on bundles, step 3 can run as a bundle `job_runs` resource instead.

## Git workflow (GitHub Actions)
```
feature branch ──PR──▶ plan: fmt, validate, plan for both stacks → plan posted on the PR (safe: names only)
                       │  read-only plan SP · AWS role with DescribeSecret only (no GetSecretValue)
merge to main ───────▶ apply (approval on the "prod" environment)
                       01-secret-scope ▶ sync secrets (AWS SM → scope) ▶ 02-federation ▶ smoke test (SHOW SCHEMAS)
manual: rotate ──────▶ sync secrets ▶ new VersionId → ALTER CONNECTION (no drop, catalog untouched)
manual: destroy ─────▶ 02-federation (catalog, connection) ▶ 01-secret-scope
```

## Security controls
- **No secrets stored in GitHub.** Databricks auth uses [GitHub OIDC workload identity federation](https://docs.databricks.com/aws/en/dev-tools/auth/provider-github)
  (`DATABRICKS_AUTH_TYPE=github-oidc`). AWS auth uses an OIDC-assumed IAM role. There are no PATs or client secrets.
- **The password lives in only two places:** AWS Secrets Manager (the source of truth) and the Databricks secret scope (encrypted).
  It isn't in `.tf` files, `tfvars`, plans, state, or logs.
- **Least privilege:** the deploy SP owns the scope (MANAGE) and creates the connection. Catalog users get `USE CATALOG / USE SCHEMA / SELECT`
  only and **never** get READ on the scope. Only the `prod` environment's role can call `GetSecretValue`.
- **Change control:** PR review plus a required approval on the `prod` environment, with one deploy at a time (concurrency lock).
  Terraform state is in an encrypted S3 bucket with DynamoDB locking.

## Prerequisites (one-time)
1. A Databricks service principal for deploys and one for plans, each with a **federation policy** for this repo. The subjects are
   `repo:<org>/<repo>:environment:prod` (deploy) and `repo:<org>/<repo>:pull_request` (plan). Repos created, renamed or transferred on or after July 15, 2026 use the
   `repo:<org>@<id>/<repo>@<id>:...` format instead. Copy the exact `sub` from GitHub.
2. The deploy SP needs `CREATE CONNECTION` and `CREATE CATALOG` on the metastore, plus a SQL warehouse it can use.
3. AWS IAM roles for GitHub OIDC. The plan role needs state read and `DescribeSecret`. The deploy role needs state read/write and `GetSecretValue` on the Redshift secret.
4. Network path from Databricks serverless compute or the warehouse to Redshift (NCC / PrivateLink / security group).
5. GitHub repository **variables** (not secrets): `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_PLAN_CLIENT_ID`,
   `WAREHOUSE_ID`, `AWS_REGION`, `AWS_PLAN_ROLE_ARN`, `AWS_DEPLOY_ROLE_ARN`, `AWS_REDSHIFT_SECRET_ID`, `TF_STATE_BUCKET`, `TF_LOCK_TABLE`.

## Notes
- **Rotation:** a new AWS secret version is picked up on the next pipeline run, which re-applies `ALTER CONNECTION ... secret()`.
  Use the manual **rotate** run for an immediate cut-over, or deploy [`rotation/`](rotation/README.md) to sync and re-alter automatically on every AWS rotation.
  Updating the scope alone is not enough: the connection keeps the value it copied at `CREATE`/`ALTER` (verified).
- **Moving off the workaround:** when #4327 ships, replace `terraform_data` with `databricks_connection` and use `terraform import`.
  The scope, catalog and grants stay as they are.
- **Sample repo:** `cicd-sample/` contains `infra/01-secret-scope`, `infra/02-federation`, `scripts/`, and `.github/workflows/redshift-federation.yml`.
