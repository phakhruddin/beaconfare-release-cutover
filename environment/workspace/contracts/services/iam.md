# IAM

Create two roles, both assumable by `ecs-tasks.amazonaws.com`:

| Role | Purpose | May | Must not |
|---|---|---|---|
| Execution role | Start supplied local images and ship logs | `logs:CreateLogStream` and `logs:PutLogEvents` on this deployment's `/ecs/<family>` log groups | ECR actions, access to the quotes table, or any other action |
| Task role | The API's identity, shared by both colors | `DescribeTable`, `GetItem`, `PutItem` and `DeleteItem` on the quotes table | Anything on any other table; any action outside DynamoDB |

No policy may grant `Action: "*"` or a service-wide wildcard such as
`dynamodb:*`, and no statement may use `Resource: "*"`.

IAM policies are recorded by this endpoint but not evaluated. They are checked
as declarations only.

The supplied images are already present in the endpoint's local Docker daemon;
they are not pulled from ECR. Do not attach the standard AWS managed ECS task
execution policy: its ECR actions exceed this task's execution-role contract.

## Manifest fields

Record in `manifest.roles`: `execution_role_arn` and `task_role_arn`.
