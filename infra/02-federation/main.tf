# Stack 02: Redshift connection, foreign catalog and grants.
#
# databricks_connection only accepts literal option values (provider issue
# #4327): "{{secrets/scope/key}}" is stored verbatim and sent to Redshift as the
# username. CREATE/ALTER CONNECTION SQL supports secret('<scope>', '<key>'), so
# the connection is managed through the SQL Statement Execution API from
# terraform_data. Only scope and key NAMES reach Terraform.
terraform {
  required_version = ">= 1.5"
  required_providers {
    databricks = { source = "databricks/databricks", version = "~> 1.138" }
  }
  backend "s3" {}
}

provider "databricks" {}

locals {
  sql     = "${path.module}/../../scripts/run_sql.sh"
  options = <<-EOT
    host '${var.redshift_host}',
    port '${var.redshift_port}',
    user secret('${var.secret_scope}', '${var.user_key}'),
    password secret('${var.secret_scope}', '${var.password_key}')
  EOT
}

# ── Connection lifecycle: create once, drop on destroy ──────────────────────
# Only immutable attributes (name, type) force replacement.
resource "terraform_data" "connection" {
  triggers_replace = {
    name = var.connection_name
    type = var.connection_type
  }
  # Kept in input (not triggers) so switching warehouses never drops the connection.
  input = { warehouse = var.warehouse_id }

  provisioner "local-exec" {
    environment = { WAREHOUSE_ID = var.warehouse_id }
    command     = "${local.sql} \"CREATE CONNECTION IF NOT EXISTS ${self.triggers_replace.name} TYPE ${self.triggers_replace.type} OPTIONS (${local.options})\""
  }

  provisioner "local-exec" {
    when        = destroy
    environment = { WAREHOUSE_ID = self.output.warehouse }
    command     = "${path.module}/../../scripts/run_sql.sh \"DROP CONNECTION IF EXISTS ${self.triggers_replace.name}\""
  }
}

# ── Connection options: updated in place with ALTER, never dropped ──────────
# Re-runs when host/port/scope/keys change, or when the secret version changes
# (secret_version is the AWS Secrets Manager VersionId, which is not sensitive),
# so a rotation is applied without recreating the connection or the catalog.
resource "terraform_data" "connection_options" {
  triggers_replace = {
    connection     = terraform_data.connection.id
    options        = local.options
    secret_version = var.secret_version
  }

  provisioner "local-exec" {
    environment = { WAREHOUSE_ID = var.warehouse_id }
    command     = "${local.sql} \"ALTER CONNECTION ${var.connection_name} OPTIONS (${local.options})\""
  }
}

# ── Foreign catalog + grants: native Terraform ──────────────────────────────
resource "databricks_catalog" "redshift" {
  name            = var.catalog_name
  connection_name = var.connection_name
  options         = { database = var.redshift_database }
  owner           = var.catalog_owner
  comment         = "Redshift federation; credentials resolved from secret scope ${var.secret_scope}"
  force_destroy   = true

  depends_on = [terraform_data.connection_options]

  lifecycle {
    replace_triggered_by = [terraform_data.connection]
  }
}

resource "databricks_grants" "redshift" {
  catalog = databricks_catalog.redshift.name
  dynamic "grant" {
    for_each = var.reader_groups
    content {
      principal  = grant.value
      privileges = ["USE_CATALOG", "USE_SCHEMA", "SELECT"]
    }
  }
}

output "catalog_name" {
  value = databricks_catalog.redshift.name
}
