resource "aws_lb" "edge" {
  name               = substr("${local.prefix}-edge", 0, 32)
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.edge.id]
  subnets            = aws_subnet.public[*].id
  tags               = merge(local.tags, { Name = "${local.prefix}-edge" })
}

resource "aws_lb_target_group" "color" {
  for_each    = toset(local.colors)
  name        = substr("${local.prefix}-${each.key}", 0, 32)
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/health/ready"
    protocol            = "HTTP"
    port                = "traffic-port"
    interval            = 5
    timeout             = 3
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  deregistration_delay = 5
  tags                 = merge(local.tags, { Name = "${local.prefix}-${each.key}" })
}

# Production always forwards to the live color, preview to the standby one.
# Swapping them is an in-place listener modification.
resource "aws_lb_listener" "production" {
  load_balancer_arn = aws_lb.edge.arn
  port              = var.production_listener_port
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.color[var.live_color].arn
  }

}

resource "aws_lb_listener" "preview" {
  load_balancer_arn = aws_lb.edge.arn
  port              = var.preview_listener_port
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.color[local.standby_color].arn
  }

}
