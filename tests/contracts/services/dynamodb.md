# DynamoDB

Create one quotes table.

| Setting | Required value |
|---|---|
| Partition key | `quote_id`, type `S`. No sort key. |
| Billing mode | `PAY_PER_REQUEST` |
| TTL | Enabled on `expires_at` |

Declare only the key attribute. The table is shared by both colors and every
release, and a release never replaces it: its name is derived from
`resource_prefix` and stays the same across deployments.

## Manifest fields

Record in `manifest.data.quotes_table`: `name` and `arn`.
