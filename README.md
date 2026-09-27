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

## Layout

| Path | Purpose |
|---|---|
| `instruction.md` | What the agent is asked to build. |
| `reasoning.md` | Design, flows and score rationale for reviewers. |
| `environment/` | Agent workspace image, supplied release images, public contracts. |
| `environment/workspace/contracts/release-process.md` | The product contract. |
| `solution/` | Reference Terraform and release controller. One correct answer, not the required layout. |
| `tests/` | Verifier image and the weighted obligation suite. `tests/application` and `tests/contracts` mirror `environment/`, because Realm uploads only `tests/` as the verifier context. Keep them in sync. |
| `tests/suite/obligations.yaml` | Single source of truth for scoring. The verifier refuses to start if its weights do not reconcile to 100. |

## Validating

```bash
rv health     # static package checks
rv oracle     # runs the reference solution through the full verifier; expect 100
```
# beaconfare-release-cutover
