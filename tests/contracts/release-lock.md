# Release Lock

Only one release controller may act on a deployment at a time. An operator
running maintenance, or a second pipeline job, holds the same lock, and
`deploy.sh` must respect it. This is part of the product contract.

## The lock table

Declare a DynamoDB table named exactly `<resource_prefix>-release-lock`
(see `services/dynamodb.md`). It holds the release record
(`lock_id = release-record`, see `release-record.md`) and at most one lock
item:

| Attribute | Type | Value |
|---|---|---|
| `lock_id` | `S` | Always `release-controller`. The table's partition key. |
| `holder` | `S` | Identifies one run of `deploy.sh`. Unique per run: never reuse a value from an earlier run. |
| `lease_expires_at` | `N` | Epoch seconds when the lease lapses: the time of acquisition plus `lock_lease_seconds` from `config.json`. |

Other attributes are allowed and ignored.

## What every run of `deploy.sh` does

1. **Acquire before acting.** When the lock table already exists, acquire
   the lock **before any change**: before writing any file in the
   submission, before `terraform init`/`apply`, before any AWS write and
   before writing the manifest. Acquire with a single conditional write that
   succeeds only if no lock item exists, or if the existing item's
   `lease_expires_at` is in the past.
   When the lock table does not exist yet (the very first deployment),
   create it with Terraform or OpenTofu first, then acquire the lock before
   any release action: staging a candidate, changing a listener or changing
   a task count.
2. **Respect a live lease.** If another holder's lease has not expired, the
   run is **refused** with exit status **`75`**, under the refusal rules in
   `release-process.md` (*Refusals*): within 30 seconds, nothing changed or
   repaired, and the existing lock item left exactly as it was. An unknown
   requested release is refused with `64` before the lock is looked at.
3. **Take over an expired lease.** An item whose `lease_expires_at` is in
   the past belongs to a controller that died. Replace it with your own and
   continue normally.
4. **Hold it for the whole run.** Keep the lock from acquisition until the
   run ends. `lock_lease_seconds` is longer than the 720-second deploy
   budget, so no renewal is needed.
5. **Always release.** When the run ends, whether it succeeded, rejected a
   candidate or failed, delete the lock item with a write conditioned on
   `holder` still being yours. A lock you no longer hold is not yours to
   delete. The one exception is a run that stops at a requested crash point
   (`crash-recovery.md`): like a killed process, it leaves its lock item in
   place, and the lease blocks other runs until it lapses or an operator
   expires it.

`destroy.sh` does not take the lock.

## Manifest fields

Record the lock table in `manifest.data.lock_table`: `name` and `arn`.
