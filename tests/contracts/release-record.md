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

`destroy.sh` removes the record with the table.
