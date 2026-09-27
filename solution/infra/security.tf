# Inline ingress/egress blocks: standalone rule resources can produce an
# invalid replacement plan against this endpoint.

resource "aws_security_group" "edge" {
  name        = "${local.prefix}-edge-sg"
  description = "Public load balancer"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Production listener"
    from_port   = var.production_listener_port
    to_port     = var.production_listener_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Preview listener"
    from_port   = var.preview_listener_port
    to_port     = var.preview_listener_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Forward to API tasks"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  tags = merge(local.tags, { Name = "${local.prefix}-edge-sg" })
}

resource "aws_security_group" "api" {
  name        = "${local.prefix}-api-sg"
  description = "API tasks of both colors"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "From the load balancer only"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.edge.id]
  }

  egress {
    description = "Cloud endpoint"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.prefix}-api-sg" })
}
