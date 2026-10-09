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

Other attributes are allowed and ignored.

## Rules

1. **Written by every run that acts.** Every run that is not refused writes
   the record before it ends, while it holds the release lock. When the run
   returns, the record describes what it deployed: production forwards to
   `live_color`, each color runs its recorded count of tasks, every color
   with a non-zero count serves its recorded release, and the last-request
   attributes equal `manifest.release.last_request`. Refused runs do not
   touch it (see *Refusals* in `release-process.md`).
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
   candidate, not a rollback target, until it passes its self-test.

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

A run that finds the record not deployable is **refused** with exit status
`65` under the refusal rules in `release-process.md`: it returns within 30
seconds, changes nothing (the record, the lock table, the submission
directory, routing and tasks included) and repairs nothing. The next run
after an operator has fixed the record proceeds normally.

`destroy.sh` removes the record with the table.
