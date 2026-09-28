# CloudWatch Logs

Every task definition sends its container output to CloudWatch Logs with the
`awslogs` driver.

On this endpoint, a task's output always lands in the log group
**`/ecs/<task definition family>`**, whatever `awslogs-group` names, and the
endpoint creates that group itself if it does not exist yet. A group it
creates that way is not in your state and survives `destroy.sh`.

So for **every task definition family you register**, create a managed log
group named exactly `/ecs/<family>` with a retention of exactly
`log_retention_days` from `config.json`, and point `awslogs-group` at it.

## Manifest fields

Record in `manifest.logs.groups` the name of every log group your task
definitions write to.
