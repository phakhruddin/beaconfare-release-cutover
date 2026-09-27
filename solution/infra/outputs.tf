output "vpc_id" { value = aws_vpc.main.id }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }

output "edge_arn" { value = aws_lb.edge.arn }
output "edge_dns_name" { value = aws_lb.edge.dns_name }
output "production_listener_arn" { value = aws_lb_listener.production.arn }
output "preview_listener_arn" { value = aws_lb_listener.preview.arn }

output "cluster_arn" { value = aws_ecs_cluster.main.arn }
output "service_arns" { value = { for c, s in aws_ecs_service.color : c => s.id } }
output "target_group_arns" { value = { for c, tg in aws_lb_target_group.color : c => tg.arn } }

output "quotes_table_name" { value = aws_dynamodb_table.quotes.name }
output "quotes_table_arn" { value = aws_dynamodb_table.quotes.arn }

output "execution_role_arn" { value = aws_iam_role.execution.arn }
output "task_role_arn" { value = aws_iam_role.task.arn }
output "log_groups" { value = [aws_cloudwatch_log_group.api.name] }

output "live_color" { value = var.live_color }
output "standby_color" { value = local.standby_color }
output "color_release" { value = local.color_release }
output "color_count" { value = local.color_count }
