# Crash Recovery

A release controller can die at any moment: the worker is preempted, the job
is cancelled, the machine loses power. The release record
(`release-record.md`) and the release lock (`release-lock.md`) exist so that
the next controller can finish the story safely. This contract defines two
**named crash points** that `deploy.sh` must honour on request, so that
recovery from the two most dangerous moments of a release can be exercised
deterministically. This is part of the product contract.

## Requesting a crash

`deploy.sh` reads the optional environment variable
`BEACONFARE_FAULT_POINT`. Unset or empty means a normal run. Otherwise it
names one crash point:

| Value | The run stops right after... | ...and before |
|---|---|---|
| `staged` | a **candidate** (case 3 in `release-process.md`) runs in the standby color at `api_desired_count` tasks, every one of them warm, and the preview listener forwards to that color and answers `GET /health/ready` with `200` from the candidate release | the candidate's self-test verdict is acted on, any listener change for promotion or rejection, any scale-in of the candidate, and any change to the release record |
| `switched` | a **promotion** or a **rollback** has changed the listeners, so production forwards to the new live color and preview to the new standby color | any change to the release record, and the manifest |

A run whose request never reaches the named point (for example `switched` on
a run that rejects its candidate, or any point on a run with no release
requested) ignores the variable and completes normally. A refused run
(*Refusals* in `release-process.md`) is refused exactly as without it.

## What a crash leaves behind

When the run reaches the named point it ends **at once** with exit status
**`137`**, as if it had been killed (`kill -KILL` of the controller process
is an acceptable way to do it), and without any cleanup:

- the release lock item is **left in place**, with this run as `holder` and
  its lease still live: a crashed controller cannot release its lock;
- the release record's attributes listed in `release-record.md` are exactly
  as they were when the run started (other attributes, for example your own
  intent markers, may have changed);
- `manifest.json` is not written;
- nothing the run did in the cloud is undone: the staged candidate keeps
  running, switched listeners stay switched.

Files in the submission directory may be in any state; they are a cache.

## Recovering

A crashed holder's lease blocks every other run until it lapses: while it is
live, a run is refused with `75` like any other live lease. An operator may
end it early by setting the lock item's `lease_expires_at` to a time in the
past; the lock item is then an expired lease (rule 3 in `release-lock.md`).

The next run that takes the lock over must restore the **recorded** state,
not the state the crashed run left in the cloud and not whatever a local
cache says. With no release requested it:

- makes production forward to the recorded live color and preview to the
  recorded standby color;
- makes each color run its recorded release at its recorded count: a
  half-finished candidate is replaced by what the record assigns to that
  color;
- does all of this without failing a production request and without
  stopping or replacing a task of the recorded live color, and without
  stopping or replacing a task of a color whose running release already
  matches the record;
- records outcome `unchanged` and keeps the record's `generation` (the five
  recorded state values end as they started);
- releases its own lock when it ends.

From then on, every rule of `release-process.md` applies as if the crashed
run had never happened.
