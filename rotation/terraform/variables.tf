variable "aws_region" {
  type = string
}

variable "databricks_host" {
  type        = string
  description = "Workspace URL, e.g. https://<workspace>.cloud.databricks.com"
}

variable "databricks_warehouse_id" {
  type        = string
  description = "SQL warehouse the automation SP can use (CAN USE) to run ALTER CONNECTION"
}

variable "databricks_sp_secret_arn" {
  type        = string
  description = "Secrets Manager ARN holding {\"client_id\",\"client_secret\"} for the automation service principal"
}

variable "secret_mappings" {
  description = "One entry per AWS secret to keep in sync"
  type = list(object({
    secret_id    = string # full ARN of the source secret
    scope        = string # Databricks secret scope
    user_key     = optional(string, "app_id")
    password_key = optional(string, "app_secret")
    connection   = string           # UC connection to refresh
    validate_sql = optional(string) # optional query through the connection
  }))
}

variable "kms_key_arns" {
  type        = list(string)
  default     = []
  description = "Customer-managed KMS keys that encrypt the secrets (leave empty for the AWS-managed key)"
}

variable "subnet_ids" {
  type        = list(string)
  default     = []
  description = "Run the Lambda in a VPC (needs egress to the Databricks workspace and Secrets Manager)"
}

variable "security_group_ids" {
  type    = list(string)
  default = []
}

variable "alarm_sns_topic_arns" {
  type    = list(string)
  default = []
}
