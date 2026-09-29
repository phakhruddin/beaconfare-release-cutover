resource "aws_dynamodb_table" "quotes" {
  name         = "${local.prefix}-quotes"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "quote_id"

  attribute {
    name = "quote_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = local.tags
}

resource "aws_cloudwatch_log_group" "api" {
  for_each          = local.families
  name              = "/ecs/${each.value}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

# The release lock (contracts/release-lock.md). Used by deploy.sh through the
# AWS CLI, never by the application: the task role has no access to it.
resource "aws_dynamodb_table" "release_lock" {
  name         = "${local.prefix}-release-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "lock_id"

  attribute {
    name = "lock_id"
    type = "S"
  }

  tags = local.tags
}
