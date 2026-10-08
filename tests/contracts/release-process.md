# Release Process

This is the product contract. BeaconFare ships new pricing releases often,
and a release that prices freight wrong is worse than no release at all. The
platform must let a release be proven on real infrastructure before any
customer request reaches it, move customers onto it without a single failed
request, and move them back instantly.

## Colors

Run the API as **two ECS services in one cluster**, called the **blue** and
**green** colors. Each color has its own target group. At any moment exactly
one color is **live** and the other is **standby**.

| Listener | Port (from `config.json`) | Default action |
|---|---|---|
| Production | `production_listener_port` | Forward to the **live** color's target group |
| Preview | `preview_listener_port` | Forward to the **standby** color's target group |

Both listeners belong to the same public load balancer. Every task sets
`DEPLOYMENT_COLOR` to the color of the service that runs it, so each response
reports the color that served it.

A color runs one release at a time, selected by the task definition its
service uses. Capacity rules:

| Situation | Live color | Standby color |
|---|---|---|
| After the first deployment | `initial_release`, `api_desired_count` tasks | `0` tasks |
| After a promotion | the promoted release, `api_desired_count` tasks | the release that was live before, `api_desired_count` tasks (warm standby) |
| After a rollback | the standby release it switched to, same tasks as before | the release that was live before, `api_desired_count` tasks |
| After a rejected candidate | unchanged | `0` tasks |

## Requesting a release

`deploy.sh` reads the requested release from the environment variable
`BEACONFARE_RELEASE`, a `version` from the `releases` list in `config.json`.

- Unset or empty: keep the currently live release. On the very first
  deployment this means `initial_release`.
- A version not in `releases`: **refuse** with exit status `64` (see
  *Refusals* below). Matching is exact and case-sensitive: `v3.5.0`,
  ` 3.5.0` or `3.5.0-rc` are not `3.5.0`.

The deployment state you remember between runs (which color is live, which
release each color runs, how many tasks each color should run) is kept in
the cloud release record defined in `release-record.md`. Local files may
cache it, but a run must be able to continue from the record alone, and a
standalone `terraform plan -refresh=false` against `infra/` must agree with
what the last run deployed.

## Refusals

A run of `deploy.sh` is **refused** when it must not act at all. There are
exactly two causes, checked in this order:

| Order | Cause | Exit status |
|---:|---|---:|
| 1 | `BEACONFARE_RELEASE` is set to a version that is not in `releases` | `64` |
| 2 | Another holder's release-lock lease is live (`release-lock.md`) | `75` |

The request is validated first, before the lock is read or taken: an unknown
release with a live foreign lease is refused with `64`, and the lease is left
exactly as it was.

A refused run:

- exits within **30 seconds**, with the status above;
- changes **nothing**: no file in the submission directory (configuration
  files, Terraform state, any local state cache and `manifest.json`
  included), no Terraform or OpenTofu command that writes state, and no AWS
  write of any kind, the release lock table and the release record included;
- **repairs nothing**, even when the cloud has drifted from the recorded
  release state: swapped listeners stay swapped and wrong task counts stay
  wrong until a run that is not refused puts them back.

Once the cause is gone, the next run behaves exactly as if the refused run had
never happened: it repairs any drift, honours its own request and records its
own outcome.

## What each request must do

The very first deployment puts `initial_release` live in one color with
`api_desired_count` tasks and leaves the other color at `0`. Its outcome is
`initial`. Let *R* be the requested release on any later run.

1. **R is already live.** Change nothing that is serving. No live task is
   stopped or replaced. Managed resources deleted since the last run are
   repaired, and production keeps answering while that happens. Outcome
   `unchanged`.
2. **R is the standby release and the standby color runs
   `api_desired_count` tasks.** This is a **rollback**. Switch production to
   the standby color by changing where the listeners forward. Do not start
   new tasks for R: the tasks that were standby become live. The color that
   was live becomes standby, still running its release at
   `api_desired_count`. Outcome `rolled_back`.
3. **Otherwise R is a candidate.** Deploy R into the standby color at
   `api_desired_count` tasks, replacing whatever the standby color ran.
   Production is not touched while this happens. Explicitly converge and
   confirm the preview listener's default action on the standby target group
   before waiting for candidate readiness or calling its self-test; do not
   assume an earlier listener attachment survived repair or drift. Then
   **verify** R (below).
   - Verification passed: **promote** R by switching both listeners, so the
     candidate color becomes live and the old live color becomes standby.
     Outcome `promoted`.
   - Verification failed, including because the candidate does not become
     ready before the controller's bounded verification wait: **reject** R.
     Production stays exactly as it was. Scale the candidate color to `0`
     tasks. `deploy.sh` still exits `0`, because rejecting an unproven release
     is the pipeline working. Outcome `rejected`.

A rejection is not terminal release state. A later request for any other
valid release stages it from the zero-capacity standby color and follows the
same proof-and-promotion path. A failed candidate must not poison a later
healthy release or cause a no-op shortcut around its verification.

## Verifying a candidate

A candidate is judged only once it is warm: a task still warming up answers
`503 warming_up` (see `runtime.md`), which is neither a pass nor a failure.

A candidate passes only if **every** candidate task answers
`GET /release/selftest` with `200` and `"passed": true`, reached through the
**preview listener**, while the candidate color's target group reports
`api_desired_count` healthy targets and every response on the preview
listener reports version *R*. `/health/ready` is not verification: a release
with a pricing regression is perfectly healthy. The self-test response names
the task that answered in `task` and in the `X-BeaconFare-Task` header, so
you can tell when every task has been covered.

One of the later releases in `releases` carries a pricing regression. Which
one is not published and changes between environments. Only its self-test
reveals it.

## The recorded state is the truth

The release state your deployment records (which color is live, which
release each color runs, how many tasks each color should run) is the source
of truth, not whatever the cloud happens to show. Listeners, services and
task counts can be changed outside `deploy.sh`, for example by an operator.
A run with no release requested puts everything back to the recorded state:
production forwards to the recorded live color, preview to the recorded
standby color, and each color runs its recorded release at its recorded task
count. It does this without failing a production request and without
stopping or replacing a task of the recorded live color. Its outcome is
`unchanged`. The same holds after a controller crashed halfway through a
release (`crash-recovery.md`): the cloud then shows the crashed run's
half-finished work, and the record still shows the state to restore.

## Production during a release

- Production answers **every** request with a non-5xx status while
  `deploy.sh` runs, for every outcome above. This includes `POST /quotes`.
  The verifier gives each request 10 seconds; a request that gets no answer
  in that time is sent once more (the endpoint host can stall briefly under
  load), and it fails if the second attempt also gets no answer, a refused
  connection or a 5xx.
- A rejected candidate never answers a production request.
- When `deploy.sh` returns after a promotion or rollback, every production
  response comes from *R*, and the preview listener serves the release that
  was live before.
- Quotes written through production before or during a release stay
  readable after it, whichever release serves them.

## Recording the outcome

`deploy.sh` records the result in `manifest.release` (see
`schemas/manifest.schema.json`): the live version and color, the standby
version and color (`null` version when the standby color runs no tasks), and
`last_request` with the requested version and its outcome.
