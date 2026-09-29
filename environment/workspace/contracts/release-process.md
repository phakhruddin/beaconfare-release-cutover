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
- A version not in `releases`: exit non-zero without changing anything.

Whatever deployment state you need to remember between runs (which color is
live, which release each color runs) is your design, but it must survive
between runs of `deploy.sh` in the same submission directory, and a
standalone `terraform plan -refresh=false` against `infra/` must agree with
what the last run deployed.

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
   - Verification failed: **reject** R. Production stays exactly as it was.
     Scale the candidate color to `0` tasks. `deploy.sh` still exits `0`,
     because rejecting a bad release is the pipeline working. Outcome
     `rejected`.

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
`unchanged`.

## Production during a release

- Production answers **every** request with a non-5xx status while
  `deploy.sh` runs, for every outcome above. This includes `POST /quotes`.
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
