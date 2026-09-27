# CloudWatch Logs

Create a log group for the API with a retention of exactly
`log_retention_days` from `config.json`. Every task definition sends its
container output there with the `awslogs` driver. You may use one group per
color instead; each must then have the same retention.

## Manifest fields

Record in `manifest.logs.groups` the name of every log group the task
definitions use.
