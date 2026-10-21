# Non-sensitive config only. Credentials live in AWS Secrets Manager.
secret_scope      = "redshift-fed-scope"
user_key          = "app_id"
password_key      = "app_secret"
connection_name   = "redshift_fed"
connection_type   = "REDSHIFT"
redshift_host     = "<cluster>.<id>.<region>.redshift.amazonaws.com"
redshift_port     = "5439"
redshift_database = "<database>"
catalog_name      = "redshift_fed"
catalog_owner     = "<uc-admins-group>"
reader_groups     = ["<redshift-readers-group>"]
