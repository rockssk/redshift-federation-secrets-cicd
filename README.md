# Sample repo: Redshift federation with secret-scope credentials, deployed from GitHub

Start with [`ONE_PAGER.md`](ONE_PAGER.md) for the why and the design. This README covers setup.

```
.github/workflows/redshift-federation.yml   PR plan → approved apply → rotate / destroy
infra/01-secret-scope/                      Terraform: secret scope + ACLs (no values)
infra/02-federation/                        Terraform: connection (terraform_data → SQL API), catalog, grants
infra/envs/prod.tfvars                      Non-sensitive config only
scripts/sync_secrets.sh                     AWS Secrets Manager → Databricks scope (masked, via stdin)
scripts/run_sql.sh                          One statement via the SQL Statement Execution API
```

## One-time setup

1. **Service principals.** Create `sp-redshift-fed-deploy` and `sp-redshift-fed-plan` in the account and add both to the workspace.
   Grant the deploy SP:
   ```sql
   GRANT CREATE CONNECTION, CREATE CATALOG ON METASTORE TO `<deploy-sp-application-id>`;
   ```
   Also give it `CAN USE` on the SQL warehouse. Give the plan SP read-only access (`BROWSE`/`USE CATALOG`) so it can refresh state.

2. **GitHub OIDC federation policies** ([docs](https://docs.databricks.com/aws/en/dev-tools/auth/provider-github)):
   ```bash
   databricks account service-principal-federation-policy create <deploy-sp-numeric-id> --json '{
     "oidc_policy": { "issuer": "https://token.actions.githubusercontent.com",
                      "audiences": ["<databricks-account-id>"],
                      "subject": "repo:<org>/<repo>:environment:prod" } }'

   databricks account service-principal-federation-policy create <plan-sp-numeric-id> --json '{
     "oidc_policy": { "issuer": "https://token.actions.githubusercontent.com",
                      "audiences": ["<databricks-account-id>"],
                      "subject": "repo:<org>/<repo>:pull_request" } }'
   ```
   Repos created, renamed or transferred on or after July 15, 2026 use a `sub` that includes numeric IDs. Copy the exact value from GitHub.

3. **AWS.** Set up the GitHub OIDC identity provider and two IAM roles:
   - `AWS_PLAN_ROLE_ARN`: `s3:GetObject`/`ListBucket` on the state bucket and `secretsmanager:DescribeSecret` on the Redshift secret.
   - `AWS_DEPLOY_ROLE_ARN`: read and write on the state bucket and lock table, plus `secretsmanager:GetSecretValue` on the Redshift secret.
     Its trust policy should be limited to `repo:<org>/<repo>:environment:prod`.

4. **AWS Secrets Manager.** Store the Redshift user as JSON, `{"username": "...", "password": "..."}`. Redshift-managed secrets already use this format.

5. **GitHub.**
   - Create an environment named `prod` with required reviewers.
   - Add these repository **variables**. None of them are secrets:
     `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_PLAN_CLIENT_ID`, `WAREHOUSE_ID`, `AWS_REGION`,
     `AWS_PLAN_ROLE_ARN`, `AWS_DEPLOY_ROLE_ARN`, `AWS_REDSHIFT_SECRET_ID`, `TF_STATE_BUCKET`, `TF_LOCK_TABLE`.

6. Fill in `infra/envs/prod.tfvars`, open a PR, review the plan comment, merge, and approve the `prod` deployment.

## Operations

| Task | How |
|---|---|
| Change host, port, scope or keys | Edit `prod.tfvars` in a PR. The merge runs `ALTER CONNECTION` in place, and the catalog isn't touched. |
| Rotate the password | Rotate it in AWS Secrets Manager, then run the workflow manually with **rotate**, or wait for the next merge. |
| Add readers | Add the group to `reader_groups` in a PR. |
| Rename the connection or change its type | Edit `prod.tfvars` in a PR. Terraform drops and recreates the connection and the catalog. |
| Tear down | Run the workflow manually with **destroy** (needs the `prod` approval). |

## Verify after the first deploy
```sql
DESCRIBE CONNECTION redshift_fed;          -- shows host/port; credentials are references, not values
SHOW SCHEMAS IN redshift_fed;              -- authenticates to Redshift
SELECT * FROM redshift_fed.<schema>.<table> LIMIT 10;
```
To confirm nothing leaked: `aws s3 cp s3://$TF_STATE_BUCKET/redshift-federation/02-federation.tfstate - | grep -c '<password>'` should print `0`.
