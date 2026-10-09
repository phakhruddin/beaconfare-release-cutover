# devcloud/beaconfare-release-cutover

A Dev Cloud reinforcement-learning environment in the **Compute and
Deployment** taxonomy. The model must build a blue/green release platform for
an ECS service on an AWS-compatible local control plane using Terraform or
OpenTofu, then operate it through promotion, rollback, a defective release,
repair and destruction, all under live production traffic.

**Taxonomy:** Compute and Deployment · **Difficulty:** hard · **Pass mark:** 100/100

## The problem

One image per release (3.4.0, 3.5.0, 3.6.0) is supplied. One of 3.5.0 / 3.6.0,
chosen at random per environment, carries a pricing regression that only
`GET /release/selftest` reveals; `/health/ready` stays green. The model
supplies:

- two ECS services (blue, green), two target groups, one ALB with a
  production listener and a preview listener that always point at opposite
  colors;
- one task definition per color and release (the emulator ignores changes to
  a registered task definition's container definitions);
- a `deploy.sh` that is a release controller: stage a candidate in the
  standby color, prove every candidate task through the preview listener,
  then promote by swapping listeners or reject by scaling the candidate to
  zero; roll back by swapping listeners onto the warm standby;
- durable release state in auto-loaded tfvars so a standalone plan agrees.

Traps that carry the difficulty:

1. **Health is not verification.** The regression is healthy. Promoting on
   target health alone ships it; the run is capped at 39.
2. **In-place updates are not blue/green.** Updating the live service's task
   definition replaces live tasks, loses the warm standby and gives no
   pre-traffic proof.
3. **Ignored container definitions.** Changing the image inside one task
   definition does nothing after the first apply.
4. **One-shot Terraform.** A single apply cannot both stage and gate a
   release; deploy must apply, verify, then apply again.
5. **State that survives.** Which color is live must persist between runs
   and match a standalone plan.
6. **Rollback must start nothing.** The verifier compares task ARNs.
7. **Warm-up is not failure.** Releases answer `503 warming_up` for a
   randomized 20–35 s after start; a self-test verdict only counts once warm.
8. **Recorded state wins over drift.** Swapped listeners and a scaled-down
   standby must be restored from the recorded state, not adopted.
9. **Rejection is recoverable.** An unready or defective candidate is rejected
   successfully; the next healthy candidate must stage from its zero-capacity
   color and still complete a verified promotion.
10. **Serialize the controller.** A DynamoDB lease (`release-lock.md`) is
    taken before any change, respected with exit `75` while another holder's
    lease is live, taken over when expired, and released on every exit.
11. **Refuse without side effects.** An unknown release exits `64` before the
    lock is read; a live foreign lease exits `75`. Either way the run returns
    within 30 s and changes and repairs nothing, not even drift; the next run
    that is not refused restores the recorded state.
12. **The release state lives in the cloud.** A release record item in the
    lock table (`release-record.md`) is written by every acting run, carries a
    generation that moves by exactly one when the state changes, and is
    enough on its own: the verifier replaces the submission directory with a
    fresh copy (only Terraform state kept) and the controller must carry on
    without starting or stopping a task.
13. **Survive a crashed controller.** `BEACONFARE_FAULT_POINT=staged|switched`
    (`crash-recovery.md`) stops a run with exit `137` right after a candidate
    is warm behind preview, or right after a promotion/rollback swapped the
    listeners. The crashed run keeps its lock and leaves the record alone;
    after the lease is expired, the next run must restore the recorded state
    (not the cache, not the cloud) without disturbing healthy colors.
14. **Operators edit the record; rollback targets must be proven.** The
    record lists `verified` releases. An operator may switch the live color,
    change a color's release or count, or revoke releases; the next run
    carries it out without disruption. A standby release that is not
    verified (for example the defective one, placed by an operator) is a
    candidate, never a rollback target. A record that is not a deployable
    state is refused with `65`.

## Layout

| Path | Purpose |
|---|---|
| `instruction.md` | What the agent is asked to build. |
| `reasoning.md` | Design, flows and score rationale for reviewers. |
| `environment/` | Agent workspace image, supplied release images, public contracts. `runtime.sh` also starts one idle `release-keeper` container per release image (labelled into the compose project, removed by `down --remove-orphans`) so no release image can be removed from the shared Docker daemon before it is deployed. |
| `environment/workspace/contracts/release-process.md` | The product contract. |
| `environment/workspace/contracts/release-record.md` | The cloud release record every acting run writes and a fresh worker continues from. |
| `environment/workspace/contracts/crash-recovery.md` | Named crash points, what a crashed run leaves behind, and takeover recovery. |
| `environment/workspace/contracts/release-lock.md` | The exclusive release lock every `deploy.sh` run takes, respects (exit 75) and releases. |
| `solution/` | Reference Terraform and release controller. One correct answer, not the required layout. |
| `tests/` | Verifier image and the weighted obligation suite. `tests/application` and `tests/contracts` mirror `environment/`, because Realm uploads only `tests/` as the verifier context. Keep them in sync. |
| `scripts/diagnose-floci-scaling.sh` | Author-only diagnostic: probes how the pinned Floci image scales ECS services from zero. Not part of the agent or verifier environment. |
| `tests/suite/obligations.yaml` | Single source of truth for scoring. The verifier refuses to start if its weights do not reconcile to 100. |

## Validating

```bash
rv health     # static package checks
rv oracle     # runs the reference solution through the full verifier; expect 100
```
# beaconfare-release-cutover
