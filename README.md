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

## Layout

| Path | Purpose |
|---|---|
| `instruction.md` | What the agent is asked to build. |
| `reasoning.md` | Design, flows and score rationale for reviewers. |
| `environment/` | Agent workspace image, supplied release images, public contracts. `runtime.sh` also starts one idle `release-keeper` container per release image (labelled into the compose project, removed by `down --remove-orphans`) so no release image can be removed from the shared Docker daemon before it is deployed. |
| `environment/workspace/contracts/release-process.md` | The product contract. |
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
