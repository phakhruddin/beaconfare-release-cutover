resource "aws_ecs_cluster" "main" {
  name = "${local.prefix}-cluster"
  tags = local.tags
}

resource "aws_ecs_task_definition" "api" {
  for_each = local.task_definitions

  family                   = "${local.prefix}-${each.value.color}-${replace(each.value.version, ".", "-")}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "api"
    image        = local.releases[each.value.version].image
    essential    = true
    portMappings = [{ containerPort = 8080, hostPort = 8080, protocol = "tcp" }]
    environment = concat(local.aws_environment, [
      { name = "QUOTES_TABLE", value = aws_dynamodb_table.quotes.name },
      { name = "DEPLOYMENT_COLOR", value = each.value.color },
    ])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = each.value.color
      }
    }
  }])

  tags = merge(local.tags, { Release = each.value.version, Color = each.value.color })

  # The endpoint returns container definitions in a different shape than
  # registered (contracts/runtime.md). Each release has its own definition,
  # so nothing ever needs to change here after registration.
  lifecycle {
    ignore_changes = [container_definitions]
  }
}

resource "aws_ecs_service" "color" {
  for_each = toset(local.colors)

  name            = "${local.prefix}-${each.key}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api["${each.key}-${local.color_release[each.key]}"].arn
  desired_count   = local.color_count[each.key]
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.color[each.key].arn
    container_name   = "api"
    container_port   = 8080
  }

  depends_on          = [aws_lb_listener.production, aws_lb_listener.preview, aws_iam_role_policy.task]
  scheduling_strategy = "REPLICA"
  tags                = merge(local.tags, { Color = each.key })

  lifecycle {
    ignore_changes = [scheduling_strategy]
  }
}
