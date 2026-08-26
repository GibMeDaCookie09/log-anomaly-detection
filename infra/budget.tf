# The brief for this project is "zero cost". That is a claim about the future,
# so it needs a detector of its own - set up on day one, before there is
# anything to detect.
#
# Two notifications, because they catch different failures:
#   ACTUAL >= 1%      - something has already cost money. At a $1 budget that
#                       fires at one cent, which is the point: the expected
#                       spend is zero, so any spend is the signal.
#   FORECASTED >= 100% - spend is on track to exceed the budget by month end,
#                       which arrives before the money does.
resource "aws_budgets_budget" "monthly" {
  count = var.budget_alert_email != "" ? 1 : 0

  name         = "${var.project_name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 1
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
