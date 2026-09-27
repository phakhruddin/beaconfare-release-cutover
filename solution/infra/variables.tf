# Runtime configuration, written by deploy.sh to config.auto.tfvars.json.
variable "region" { type = string }
variable "aws_endpoint_url" { type = string }
variable "resource_prefix" { type = string }
variable "api_desired_count" { type = number }
variable "production_listener_port" { type = number }
variable "preview_listener_port" { type = number }
variable "log_retention_days" { type = number }
variable "initial_release" { type = string }

variable "releases" {
  type = list(object({
    version  = string
    image    = string
    image_id = string
  }))
}

# Release state, written by deploy.sh to release.auto.tfvars.json. It is the
# durable record of which color is live and what each color runs, so a
# standalone plan agrees with the last deployment.
variable "live_color" {
  type    = string
  default = "blue"
  validation {
    condition     = contains(["blue", "green"], var.live_color)
    error_message = "live_color must be blue or green."
  }
}

variable "color_release" {
  description = "Release version each color's service runs."
  type        = map(string)
  default     = {}
}

variable "color_count" {
  description = "Desired task count of each color."
  type        = map(number)
  default     = {}
}
