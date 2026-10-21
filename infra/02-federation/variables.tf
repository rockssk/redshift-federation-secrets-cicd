variable "warehouse_id" {
  description = "SQL warehouse that runs CREATE/ALTER/DROP CONNECTION"
  type        = string
}

variable "secret_scope" {
  type = string
}

variable "user_key" {
  type    = string
  default = "app_id"
}

variable "password_key" {
  type    = string
  default = "app_secret"
}

variable "secret_version" {
  description = "AWS Secrets Manager VersionId of the Redshift secret; a change re-applies the connection options"
  type        = string
  default     = "initial"
}

variable "connection_name" {
  type = string
}

variable "connection_type" {
  type    = string
  default = "REDSHIFT"
}

variable "redshift_host" {
  type = string
}

variable "redshift_port" {
  type    = string
  default = "5439"
}

variable "redshift_database" {
  type = string
}

variable "catalog_name" {
  type = string
}

variable "catalog_owner" {
  description = "Group that owns the catalog (avoid individual users)"
  type        = string
}

variable "reader_groups" {
  type    = list(string)
  default = []
}
