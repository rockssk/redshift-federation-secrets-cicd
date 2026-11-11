aws_region               = "us-east-1"
databricks_host          = "https://<workspace>.cloud.databricks.com"
databricks_warehouse_id  = "<warehouse-id>"
databricks_sp_secret_arn = "arn:aws:secretsmanager:us-east-1:<acct>:secret:databricks/secret-sync-sp-XXXXXX"

secret_mappings = [
  {
    secret_id    = "arn:aws:secretsmanager:us-east-1:<acct>:secret:redshift/fed-XXXXXX"
    scope        = "redshift-fed-scope"
    connection   = "redshift_fed"
    validate_sql = "SELECT 1 FROM redshift_fed.public.<table> LIMIT 1"
  },
]

alarm_sns_topic_arns = ["arn:aws:sns:us-east-1:<acct>:data-platform-alerts"]
