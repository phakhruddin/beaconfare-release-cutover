# Infrastructure

Declare every required cloud resource in Terraform or OpenTofu and keep it in
`infra/terraform.tfstate`. Each linked service contract defines the required
settings and relationships for that resource.

## Shared Rules

- Configure the AWS provider and every AWS CLI call with `aws_endpoint_url` and
  `region` from `/workspace/config/config.json`. Use the AWS credentials already
  present in the environment.
- Use `resource_prefix` from `config.json` in the name of every managed
  resource, and tag every taggable resource with
  `BeaconFareDeployment=<resource_prefix>`.
- Create and manage only resources belonging to this deployment. Do not adopt
  or modify resources that already exist, in particular anything named
  `<resource_prefix>-legacy-*`.
- Resources created only with the AWS CLI are not accepted. You may use the CLI
  to inspect health, fetch identifiers and call the API.

## Service Contracts

| Contract | Responsibility |
|---|---|
| [`release-process.md`](release-process.md) | Colors, release requests, verification, promotion, rollback, rejection. **The product contract.** |
| [`runtime.md`](runtime.md) | The release images, their environment and the endpoint behavior that affects deployment |
| [`services/vpc.md`](services/vpc.md) | VPC, subnets, routing and security groups |
| [`services/alb.md`](services/alb.md) | Load balancer, both listeners, both target groups |
| [`services/ecs.md`](services/ecs.md) | Cluster, color services, task definitions, readiness |
| [`services/dynamodb.md`](services/dynamodb.md) | Quotes table |
| [`services/iam.md`](services/iam.md) | Role separation and least privilege |
| [`services/cloudwatch-logs.md`](services/cloudwatch-logs.md) | Log groups and retention |

## Required Result

```text
                 public ALB (internet-facing)
        ┌───────────────────────┴───────────────────────┐
  production listener                              preview listener
  (production_listener_port)                       (preview_listener_port)
        │ forwards to the LIVE color                    │ forwards to the STANDBY color
        ▼                                               ▼
  ┌──────────────────────┐                   ┌──────────────────────┐
  │ blue target group    │ ◄── swap on ──►   │ green target group   │
  │ blue ECS service     │    promote or     │ green ECS service    │
  │ (private subnets)    │    rollback       │ (private subnets)    │
  └──────────┬───────────┘                   └──────────┬───────────┘
             └──────────────► quotes table ◄────────────┘
```
