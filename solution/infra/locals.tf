locals {
  prefix = var.resource_prefix
  colors = ["blue", "green"]

  tags = {
    BeaconFareDeployment = var.resource_prefix
  }

  azs = ["${var.region}a", "${var.region}b"]

  standby_color = var.live_color == "blue" ? "green" : "blue"

  releases = { for r in var.releases : r.version => r }

  # What each color runs. Before the first release state exists, the live
  # color runs the initial release and the standby color is idle.
  color_release = {
    for c in local.colors : c => lookup(var.color_release, c, var.initial_release)
  }
  color_count = {
    for c in local.colors : c => lookup(var.color_count, c, c == var.live_color ? var.api_desired_count : 0)
  }

  # One task definition per color and release. The endpoint does not echo
  # container definitions back in the shape they were registered, so they are
  # ignored after creation; a release therefore never mutates an existing
  # task definition, it selects its own.
  task_definitions = {
    for pair in setproduct(local.colors, keys(local.releases)) :
    "${pair[0]}-${pair[1]}" => { color = pair[0], version = pair[1] }
  }

  aws_environment = [
    { name = "AWS_ENDPOINT_URL", value = var.aws_endpoint_url },
    { name = "AWS_REGION", value = var.region },
    { name = "AWS_ACCESS_KEY_ID", value = "test" },
    { name = "AWS_SECRET_ACCESS_KEY", value = "test" },
  ]
}
