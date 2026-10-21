variable "secret_scope" {
  type = string
}

variable "scope_readers" {
  description = "Extra principals that may READ the scope (not needed for catalog users)"
  type        = list(string)
  default     = []
}
