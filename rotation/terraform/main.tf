# Event-driven rotation sync: AWS Secrets Manager → Databricks secret scope →
# ALTER CONNECTION. No human step, and no secret value in Terraform state:
# Terraform only references secret ARNs, never reads their values.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws     = { source = "hashicorp/aws", version = ">= 5.0" }
    archive = { source = "hashicorp/archive", version = ">= 2.4" }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name          = "databricks-secret-sync"
  source_arns   = [for m in var.secret_mappings : m.secret_id]
  mappings_json = jsonencode(var.secret_mappings)
}

data "archive_file" "lambda" {
  type        = "zip"
  source_file = "${path.module}/../lambda/handler.py"
  output_path = "${path.module}/build/handler.zip"
}

# ── IAM: read ONLY the mapped secrets + the SP credential secret ────────────
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "ReadMappedSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = concat(local.source_arns, [var.databricks_sp_secret_arn])
  }
  dynamic "statement" {
    for_each = length(var.kms_key_arns) > 0 ? [1] : []
    content {
      sid       = "DecryptSecrets"
      actions   = ["kms:Decrypt"]
      resources = var.kms_key_arns
    }
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }
  statement {
    sid       = "DeadLetter"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dlq.arn]
  }
}

resource "aws_iam_role_policy" "lambda" {
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 90
}

resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name}-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_lambda_function" "sync" {
  function_name    = local.name
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 300
  memory_size      = 256

  # Only identifiers and non-sensitive config: no credential values.
  environment {
    variables = {
      DATABRICKS_HOST         = var.databricks_host
      DATABRICKS_SP_SECRET_ID = var.databricks_sp_secret_arn
      DATABRICKS_WAREHOUSE_ID = var.databricks_warehouse_id
      SECRET_MAPPINGS         = local.mappings_json
    }
  }

  dead_letter_config {
    target_arn = aws_sqs_queue.dlq.arn
  }

  dynamic "vpc_config" {
    for_each = length(var.subnet_ids) > 0 ? [1] : []
    content {
      subnet_ids         = var.subnet_ids
      security_group_ids = var.security_group_ids
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda]
}

# Retries for transient failures (e.g. warehouse cold start); then DLQ.
resource "aws_lambda_function_event_invoke_config" "sync" {
  function_name                = aws_lambda_function.sync.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600
}

# ── EventBridge: fire on rotation / value change of the mapped secrets ──────
# Secrets Manager events reach EventBridge through CloudTrail (management
# events must be enabled in the account, which is the default trail behaviour).
resource "aws_cloudwatch_event_rule" "secret_changed" {
  name        = "${local.name}-secret-changed"
  description = "Sync rotated Secrets Manager values into Databricks"
  event_pattern = jsonencode({
    source = ["aws.secretsmanager"]
    detail = {
      eventSource = ["secretsmanager.amazonaws.com"]
      eventName   = ["RotationSucceeded", "PutSecretValue", "UpdateSecret"]
    }
  })
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule = aws_cloudwatch_event_rule.secret_changed.name
  arn  = aws_lambda_function.sync.arn
}

resource "aws_lambda_permission" "events" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sync.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.secret_changed.arn
}

# ── Alerting: page someone if a sync fails ──────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${local.name}-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.sync.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_sns_topic_arns
}

output "lambda_function_name" { value = aws_lambda_function.sync.function_name }
output "dead_letter_queue_url" { value = aws_sqs_queue.dlq.url }
