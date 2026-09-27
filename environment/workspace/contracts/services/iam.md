# IAM

Create two roles, both assumable by `ecs-tasks.amazonaws.com`:

| Role | Purpose | May | Must not |
|---|---|---|---|
| Execution role | Pull images, ship logs | Write to this deployment's log groups | Access the quotes table |
| Task role | The API's identity, shared by both colors | `DescribeTable`, `GetItem`, `PutItem` and `DeleteItem` on the quotes table | Anything on any other table; any action outside DynamoDB |

No policy may grant `Action: "*"` or a service-wide wildcard such as
`dynamodb:*`, and no statement may use `Resource: "*"`.

IAM policies are recorded by this endpoint but not evaluated. They are checked
as declarations only.

## Manifest fields

Record in `manifest.roles`: `execution_role_arn` and `task_role_arn`.
