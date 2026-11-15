# Shared durable state: LangGraph checkpoints + run index (PK="RUNS"), read by any AgentCore session.
resource "aws_dynamodb_table" "state" {
  name         = "${var.name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

data "aws_iam_policy_document" "state" {
  statement {
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
      "dynamodb:Query", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem",
    ]
    resources = [aws_dynamodb_table.state.arn]
  }
}

resource "aws_iam_role_policy" "state" {
  name   = "state"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.state.json
}

output "state_table" {
  value = aws_dynamodb_table.state.name
}
