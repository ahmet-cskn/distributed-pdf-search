# Safety net: email alerts when this account's monthly AWS usage gets close
# to the limit. Expected usage is a few cents per month.

resource "aws_budgets_budget" "monthly" {
  name         = "${var.project_name}-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Track usage before credits are applied. Otherwise free-tier credits would
  # hide all spending and the alert could only fire once they are used up.
  cost_types {
    include_credit = false
  }

  # Usage has actually passed 80% of the limit.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  # AWS forecasts that usage will pass the limit by the end of the month.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
