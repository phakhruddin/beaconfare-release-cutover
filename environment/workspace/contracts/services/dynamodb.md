# DynamoDB

Create two tables: the quotes table and the release lock table.

## Quotes table

| Setting | Required value |
|---|---|
| Partition key | `quote_id`, type `S`. No sort key. |
| Billing mode | `PAY_PER_REQUEST` |
| TTL | Enabled on `expires_at` |

Declare only the key attribute. The table is shared by both colors and every
release, and a release never replaces it: its name is derived from
`resource_prefix` and stays the same across deployments.

## Release lock table

| Setting | Required value |
|---|---|
| Name | Exactly `<resource_prefix>-release-lock` |
| Partition key | `lock_id`, type `S`. No sort key. |
| Billing mode | `PAY_PER_REQUEST` |

Declare only the key attribute. How the lock is used is defined in
[`../release-lock.md`](../release-lock.md); the same table holds the release
record defined in [`../release-record.md`](../release-record.md). The lock table is used by
`deploy.sh`, never by the application.

## Manifest fields

Record in `manifest.data.quotes_table` and `manifest.data.lock_table`:
`name` and `arn` of each.
