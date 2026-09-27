# ECS

Create one ECS cluster and two services, one per color (see
[`../release-process.md`](../release-process.md)).

| Setting | Required value |
|---|---|
| Launch type | `FARGATE`, `awsvpc` networking |
| Subnets | Private subnets only, public IP assignment disabled |
| Load balancer | The color's own target group, container port `8080` |
| Task role | The task role in `iam.md` |
| Execution role | The execution role in `iam.md` |
| `DEPLOYMENT_COLOR` | The service's color |

Task definitions must select the supplied images by the `image` references in
`config.json`. The environment each image requires is in
[`../runtime.md`](../runtime.md). A color changes release by changing which
task definition its service uses.

## Readiness

`deploy.sh` returns only when:

- the production listener forwards to the live color, and every response it
  gives reports the live release;
- the live color's target group reports `api_desired_count` healthy targets
  and the live service runs `api_desired_count` tasks;
- the standby color runs the number of tasks `release-process.md` requires
  for the outcome, and when that number is not zero the preview listener
  serves the standby release from `api_desired_count` healthy targets.

## Manifest fields

Record in `manifest.compute`:

| Field | Required value |
|---|---|
| `cluster_arn` | ECS cluster ARN. |
| `services` | Object mapping `blue` and `green` to that service's ARN. |
| `target_groups` | Object mapping `blue` and `green` to that color's target group ARN. |
