# Stack 01: secret scope and its ACLs. The scope holds the Redshift credentials;
# the VALUES are written by scripts/sync_secrets.sh from AWS Secrets Manager,
# never by Terraform, so they are not in .tf files, plans, or state.
terraform {
  required_version = ">= 1.5"
  required_providers {
    databricks = { source = "databricks/databricks", version = "~> 1.138" }
  }
  backend "s3" {} # bucket/key/region/dynamodb_table passed with -backend-config in CI
}

provider "databricks" {} # CI: DATABRICKS_HOST + DATABRICKS_CLIENT_ID + DATABRICKS_AUTH_TYPE=github-oidc

resource "databricks_secret_scope" "redshift" {
  name = var.secret_scope
}

# The CI service principal creates the scope, so it already has MANAGE on it.
# Everyone else gets READ only if explicitly listed (normally nobody).
resource "databricks_secret_acl" "readers" {
  for_each   = toset(var.scope_readers)
  scope      = databricks_secret_scope.redshift.name
  principal  = each.value
  permission = "READ"
}

output "secret_scope" {
  value = databricks_secret_scope.redshift.name
}
