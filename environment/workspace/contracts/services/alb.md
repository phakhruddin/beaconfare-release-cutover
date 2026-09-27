# Application Load Balancer

Create one internet-facing application load balancer in the public subnets.

| Resource | Required settings |
|---|---|
| Blue target group | Target type `ip`, protocol `HTTP`, port `8080`, health check path `/health/ready` |
| Green target group | Same settings as blue |
| Production listener | `HTTP` on `production_listener_port`, default action forwards to the live color's target group |
| Preview listener | `HTTP` on `preview_listener_port`, default action forwards to the standby color's target group |

Each listener's default action forwards to exactly one target group, and the
two listeners always forward to different target groups. Do not split
production traffic across both colors.

## Reaching the load balancer

Listener sockets are served by the cloud endpoint host. From the workspace
and from the verifier, a listener is reached at
`http://<host of aws_endpoint_url>:<listener port>` with the `Host` header set
to the load balancer's generated DNS name. That DNS name is not itself
resolvable.

## Manifest fields

Record in `manifest.edge`:

| Field | Required value |
|---|---|
| `load_balancer_arn` | Load balancer ARN. |
| `dns_name` | Generated DNS name. |
| `host_header` | The `Host` value that selects this load balancer, its DNS name. |
| `production_listener_arn` | Production listener ARN. |
| `production_url` | `http://<host of aws_endpoint_url>:<production_listener_port>` |
| `preview_listener_arn` | Preview listener ARN. |
| `preview_url` | `http://<host of aws_endpoint_url>:<preview_listener_port>` |
