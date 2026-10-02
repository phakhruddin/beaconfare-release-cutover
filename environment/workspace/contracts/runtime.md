# Runtime

One image is supplied per release and all of them are already built into the
local Docker daemon used by the cloud endpoint. Do not rebuild, replace or
modify them. Their versions, references and image IDs are the `releases` list
in `/workspace/config/config.json`.

Each image has its release version and its pricing build fixed inside it.
Nothing you set on a task definition changes either.

## Environment

| Variable | Required value |
|---|---|
| `AWS_ENDPOINT_URL` | `aws_endpoint_url` from `config.json`. Tasks resolve that host. |
| `AWS_REGION` | `region` from `config.json`. |
| `AWS_ACCESS_KEY_ID` | `test` |
| `AWS_SECRET_ACCESS_KEY` | `test` |
| `QUOTES_TABLE` | Name of the quotes table (see `services/dynamodb.md`). |
| `DEPLOYMENT_COLOR` | `blue` or `green`: the color of the service running the task. |
| `PORT` | Optional. Defaults to `8080`. |

An image that is missing a required variable, or has a `DEPLOYMENT_COLOR`
other than `blue` or `green`, exits at start and logs `config_missing` or
`config_invalid`.

## Behavior

The HTTP API is in `openapi.yaml`. It listens on `8080`.

- Every response carries `X-BeaconFare-Version` (the release), 
  `X-BeaconFare-Color` (the task's `DEPLOYMENT_COLOR`) and
  `X-BeaconFare-Task` (the task's identity).
- **Warm-up.** Every release loads its pricing cache after the process
  starts, which takes 20–35 seconds. The exact time is fixed in the image
  but is not published in `config.json`. Until it finishes, `/health/ready`,
  `/release/selftest` and `POST /quotes` answer `503` with code
  `warming_up`. `/health/live` and `GET /release` answer at once.
- `GET /health/ready` answers `200` when warm-up is over and the quotes table
  exists and is `ACTIVE`, otherwise `503`. It says nothing about pricing
  correctness.
- `GET /release/selftest` prices a fixed set of reference lanes, writes,
  reads back and deletes a probe item in the quotes table, and answers `200`
  with `"passed": true` only if all of that is correct. Otherwise it answers
  `500` with `"passed": false` and the failures. Probe items carry
  `expires_at`. A `503` with code `warming_up` is not a result: the task
  has not finished warming up and must be asked again later.
- `POST /quotes` prices a lane and stores the quote in the quotes table.
  `GET /quotes/{quote_id}` reads it back. Any release reads quotes written by
  any other release.

## Logging

The image writes one JSON object per line to stdout.

## Emulator behavior that affects deployment

- **Resource refreshes may report non-material drift.** The endpoint does not
  return every field AWS returns. Use `terraform plan -refresh=false` when
  checking whether configuration and state agree.
- **Some attributes cannot be read back in the shape they were written.** The
  difference is recorded in state as soon as `apply` finishes, so every later
  plan wants to **replace** the resource.

  | Resource | Attribute | What happens |
  |---|---|---|
  | `aws_ecs_task_definition` | `container_definitions` | Returned in a different shape than registered. |
  | `aws_ecs_service` | `scheduling_strategy` | Not echoed back. |

  `logConfiguration` is among what is not returned: see
  `services/cloudwatch-logs.md` for where logs actually go.

  Declare both normally, then add a **narrow**
  `lifecycle { ignore_changes = [...] }` for each so redeployment stays
  stable. Understand the consequence: once a task definition is registered,
  a later change to its `container_definitions`, the image included, is
  ignored by Terraform. A release that needs a different image needs a task
  definition of its own.
- **ECS deployments are simplified: a new task definition does not replace
  running tasks.** When the task definition of a service that is running
  tasks changes (through Terraform, OpenTofu or the API), the service records
  the new task definition, but the tasks already running keep running the
  old one; no replacement deployment happens. Tasks the service starts
  afterwards, for example when it is scaled up from `0`, use the new task
  definition. To move a color that runs tasks onto another release, scale it
  to `0`, wait until it runs no tasks, then scale it back up.
  `deploymentConfiguration`, minimum and maximum percent, health check grace
  periods, the deployment circuit breaker and automatic rollback are
  recorded but **not acted on**. Changing only `desired_count` does not
  replace running tasks either.
- **A Terraform or OpenTofu update of an existing service's
  `desired_count` is not applied.** The apply succeeds and state records the
  new value, but the service keeps its previous desired count. The count a
  service is **created** with is honored, and so is
  `aws ecs update-service --desired-count` on an existing service. Changing
  the task definition through Terraform or OpenTofu works normally.
- **Each listener port is one socket on the endpoint host.** A listener on
  port *P* is reached at `http://<host of aws_endpoint_url>:P`. When only one
  listener uses a port, any `Host` header reaches it.
- **Security groups and IAM policies are recorded, not enforced.** They are
  checked as declarations only.
- **Standalone security group rules may produce an invalid replacement plan.**
  Define ingress and egress as inline blocks on the security group resource.
