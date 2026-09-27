###############################################################################
# dynamodb.tf
#
# The single-table `ask-gulf-leads` store (R17.1, R14.4). It holds both lead
# records and draft application records, distinguished by a `pk` prefix and an
# `entity_type` attribute (see design Data Models). Partition key `pk` (string),
# no sort key. On-demand (PAY_PER_REQUEST) billing is fine for the demo.
###############################################################################

resource "aws_dynamodb_table" "leads" {
  name         = var.table_name
  billing_mode = var.dynamodb_billing_mode
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  # Point-in-time recovery is cheap insurance; safe to leave on for a demo.
  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Name = var.table_name
  }
}
