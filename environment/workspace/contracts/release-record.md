# Release Record

The release state (which color is live, which release each color runs and
how many tasks each color should run) lives **in the cloud**, in a release
record. Files in the submission directory are only a cache of it: a release
controller running on a fresh worker must be able to continue from the
record alone. This is part of the product contract.

## The record

The record is one item in the release lock table
(`<resource_prefix>-release-lock`, see `release-lock.md`), next to the lock
item:

| Attribute | Type | Value |
|---|---|---|
| `lock_id` | `S` | Always `release-record`. |
| `generation` | `N` | Version of the recorded state, see below. |
| `live_color` | `S` | `blue` or `green`: the color production forwards to. |
| `blue_release`, `green_release` | `S` | The release each color is assigned. For a color whose count is `0` the value is yours to choose and is not checked. |
| `blue_count`, `green_count` | `N` | The task count each color should run. |
| `last_request_version` | `S` | The version the last run acted on: the requested release, or the live release when none was requested. |
| `last_request_outcome` | `S` | That run's outcome, as in `manifest.release.last_request.outcome`. |
| `verified` | `L` of `S` | The releases this deployment has proven: `initial_release` from the first deployment on, plus every release promoted after passing its self-test. A release whose self-test fails is removed. Order and duplicates are not significant. Only a release in this list may be rolled back to (`release-process.md`). |
| `blue_proof_release`, `green_proof_release` | `S` | The release whose current tasks were last proven for that color, or the empty string when the color has no current-task proof. |
| `blue_proof_tasks`, `green_proof_tasks` | `L` of `S` | The distinct `X-BeaconFare-Task` identities that passed that color's self-test, sorted lexically. The list is empty when there is no current-task proof. |

Other attributes are allowed and ignored.

## Rules

1. **Written by every run that acts.** Every run that is not refused writes
   the record before it ends, while it holds the release lock. When the run
   returns, the record describes what it deployed: production forwards to
   `live_color`, each color runs its recorded count of tasks, every color
   with a non-zero count serves its recorded release, and the last-request
   attributes equal `manifest.release.last_request`. The live color has a
   proof for exactly its recorded release and its `api_desired_count` current
   task identities. An idle color has an empty proof. A running standby may
   have either an exact proof or an empty proof; an empty proof means it is
   not a rollback target. Refused runs do not touch it (see *Refusals* in
   `release-process.md`).
2. **Generation.** The first deployment records `generation` `1`. After
   that, a run that ends with a different `live_color`, `blue_release`,
   `green_release`, `blue_count` or `green_count` than the record held when
   the run started records the previous generation **plus exactly one**,
   however many times it writes the record along the way. A run that ends
   with the same five values keeps the generation unchanged.
3. **The record is the source of truth.** Between any two runs, the
   submission directory may be replaced by a fresh copy of the submission
   as you handed it over, keeping only `infra/terraform.tfstate`. Every
   other file you wrote at run time (your own state files, generated
   variable files, `manifest.json`, logs) is gone, and files you handed
   over may be stale. The next run must continue exactly as if nothing had
   been lost: with no release requested it changes nothing that is serving
   and starts or stops no task, a warm standby stays warm and can still be
   rolled back to, and the standalone plan still shows nothing to create or
   delete.

4. **A crash leaves the record behind.** A run that stops at a requested
   crash point (`crash-recovery.md`) does not change the attributes above.
   The next run that takes over the lock restores what the record says, not
   what the crashed run left in the cloud or in local files.

5. **Operators may edit the record.** Between runs (never while a run
   holds the lock) an operator may change the record directly: point
   `live_color` at the other color, change a color's release or count, or
   remove releases from `verified` so they must be proven again. An
   operator who edits the record raises `generation` by one. The next run
   treats the edited record as the recorded state and carries it out like
   any drift (*The recorded state is the truth* in `release-process.md`):
   listeners switch to the recorded colors without a failed production
   request, a color whose recorded release or count differs from what it
   runs is brought to it (a color that runs tasks of another release is
   replaced, see `runtime.md`), no task of a color that already runs its
   recorded release at its recorded count is started or stopped, and with
   no release requested the outcome is `unchanged` and the generation stays
   as the operator left it. A run never adds a release to `verified` just
   because it is running: an operator-placed standby release is a
   candidate, not a rollback target, until it passes its self-test. An
   operator need not maintain the proof attributes. If its state edit changes
   a color's release, count or tasks, the controller treats that color's old
   proof as stale and clears it; it does not manufacture a replacement proof
   merely by observing a running standby. A request must self-test those
   current standby tasks before promotion.

6. **Proofs bind verification to current warm tasks.** A proof is current
   only when its release equals the color's recorded release, it contains
   exactly `api_desired_count` distinct non-empty identities, and a fresh
   `GET /release` sample through that color's listener observes exactly that
   set with the expected release and color. Before a rollback the controller
   must make this comparison through preview. It may not substitute ECS
   desired count, target health, a task definition ARN, or `verified` for the
   task identities. When a candidate passes, the controller retains the
   exact self-tested set to authorize promotion and commits it with the final
   promoted record; the named `switched` crash point still leaves the prior
   record intact. A missing, malformed or stale
   proof is recoverable rather than an undeployable core state: it is cleared
   when the controller acts and routes a release request through candidate
   verification. The first deployment, or a recorded live color whose tasks
   were externally replaced, records a live proof after self-testing every
   current live task through production.

## Validity

A run that reads the record (that is, every run that is not refused with
`64` or `75`, except the very first deployment) first checks that the record
is a state it can deploy. The record is **not deployable** when any of these
holds:

- `live_color` is not `blue` or `green`;
- `generation` is not a whole number of at least `1`;
- `verified` is missing, empty, or not a list of strings;
- a count is anything other than `0` or `api_desired_count`;
- the live color's count is `0`;
- a color with a non-zero count is assigned a release that is not in
  `releases`;
- the live color's release is not in `verified`.

Missing, malformed or stale proof attributes alone do **not** make the core
release state undeployable. They remove rollback eligibility as rule 6 says;
the next acting run clears stale standby evidence or rebuilds evidence for the
already verified live color. This distinction lets operators recover a
recreated warm standby without a refusal or an unsafe rollback.

A run that finds the record not deployable is **refused** with exit status
`65` under the refusal rules in `release-process.md`: it returns within 30
seconds, changes nothing (the record, the lock table, the submission
directory, routing and tasks included) and repairs nothing. The next run
after an operator has fixed the record proceeds normally.

`destroy.sh` removes the record with the table.
