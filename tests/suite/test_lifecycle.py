"""Lifecycle plane.

Releases, rollback, rejection, repair, plan stability and destruction, each
run under continuous production traffic. Faults are limited to what the
public contract describes: requesting a release, deleting managed resources
and destroying. Every check reads routing, tasks and responses live.
"""
from __future__ import annotations

import time

from .test_live import live_color, other, sample
import hashlib
import shutil
import threading

import boto3

from .tools.aws import BOTO_CONFIG
from .tools.config import _writable_copy
from .tools.deployment import deploy, deploy_raw, destroy
from .tools.errors import CleanupLeak, DefectiveServed, HarnessError, SubmissionFailure
from .tools.results import CheckResult, Outcome
from .tools.terraform import describe_changes, disruptive_changes, plan
from .tools.traffic import Traffic, TrafficReport
from .tools.trial import TrialContext, obligation


def release_under_traffic(trial: TrialContext, release: str | None, label: str) -> tuple[TrafficReport, BaseException | None]:
    """Run deploy.sh while production traffic flows. Never raises for deploy."""
    failure: BaseException | None = None
    with Traffic(trial.production) as traffic:
        time.sleep(2)
        try:
            deploy(trial.config.submission_dir, trial.config.logs_dir, label=label, release=release)
        except SubmissionFailure as exc:
            failure = exc
        time.sleep(2)
    report = traffic.report
    trial.facts.setdefault("quote_ids", []).extend(report.quote_ids())
    try:
        trial.reload_manifest()
    except Exception:  # noqa: BLE001 - judged by the caller
        pass
    return report, failure


def serving(trial: TrialContext, api, version: str, color: str, count: int) -> list[str]:
    wrong = [(r.status or r.error, r.version, r.color) for r in sample(api, count)
             if r.status != 200 or r.version != version or r.color != color]
    return wrong


def traffic_problems(report: TrafficReport, allowed: set[str]) -> list[str]:
    problems = []
    if report.total == 0:
        problems.append("no production traffic could be sent during deploy")
    failed = report.failures()
    if failed:
        problems.append(f"{len(failed)} of {report.total} production requests failed during deploy: {report.describe()}")
    unexpected = report.versions() - allowed
    if unexpected:
        problems.append(f"production was answered by unexpected release(s) {sorted(unexpected)}")
    return problems


def single_changeover(report: TrafficReport, old: str, new: str) -> bool:
    versions = [s.version for s in report.samples if s.version]
    if new not in versions:
        return True
    first_new = versions.index(new)
    return old not in versions[first_new:]


@obligation("lifecycle.zero_downtime_promotion")
def test_zero_downtime_promotion(trial: TrialContext) -> CheckResult:
    """A verified release goes live with no failed request and a warm standby."""
    cfg, cloud = trial.config, trial.cloud
    good = cfg.good_release
    color = live_color(trial)
    old = trial.manifest["release"]["live_version"]
    before = cloud.running_tasks(trial.cluster, trial.service_arn(color))

    report, failure = release_under_traffic(trial, good, "deploy-promote")
    if failure:
        raise failure
    problems = traffic_problems(report, {old, good})
    if not single_changeover(report, old, good):
        problems.append(f"production switched back to {old} after first serving {good}")

    new_color = live_color(trial)
    if new_color != other(color):
        problems.append(f"production forwards to {new_color}, expected the other color {other(color)}")
    wrong = serving(trial, trial.production, good, other(color), cfg.desired * 6)
    if wrong:
        problems.append(f"after deploy production answered {wrong[:4]}, expected {good}/{other(color)}")
    wrong = serving(trial, trial.preview, old, color, cfg.desired * 6)
    if wrong:
        problems.append(f"after deploy preview answered {wrong[:4]}, expected the warm standby {old}/{color}")
    after = cloud.wait_running(trial.cluster, trial.service_arn(color), len(before))
    if after != before:
        problems.append(f"the previously live {color} tasks were not kept as standby "
                        f"({len(before & after)} of {len(before)} still run, {len(after - before)} new)")
    live_now = cloud.wait_running(trial.cluster, trial.service_arn(other(color)), cfg.desired)
    if len(live_now) != cfg.desired:
        problems.append(f"the live {other(color)} service runs {len(live_now)} tasks, expected {cfg.desired}")

    early_quotes = trial.facts.get("early_quotes", [])
    lost = [q for q in trial.facts.get("quote_ids", [])
            if trial.production.get(f"/quotes/{q}").status != 200]
    corrupted = []
    for quote in early_quotes:
        read = trial.production.get(f"/quotes/{quote['quote_id']}")
        if read.status != 200:
            lost.append(quote["quote_id"])
            continue
        body = read.json()
        if (body.get("fare_cents"), body.get("priced_by")) != (quote["fare_cents"], quote["priced_by"]):
            corrupted.append(quote["quote_id"])
    if lost:
        problems.append(f"{len(lost)} quotes written before or during the release no longer read back")
    if corrupted:
        problems.append(f"{len(corrupted)} quotes written before the release changed fare or priced_by after cutover")

    release = trial.manifest["release"]
    if (release["live_version"], release["live_color"], release["standby_version"], release["last_request"]["outcome"]) \
            != (good, other(color), old, "promoted"):
        problems.append(f"the manifest release block is {release}")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.zero_downtime_promotion", Outcome.PASS,
        f"{good} was promoted into {other(color)} across {report.total} production requests with none failing; "
        f"{old} stayed warm in {color}",
        details={"requests": report.total, "versions": sorted(report.versions())},
    )


@obligation("lifecycle.instant_rollback")
def test_instant_rollback(trial: TrialContext) -> CheckResult:
    """Rolling back to the warm standby swaps traffic and starts nothing."""
    cfg, cloud = trial.config, trial.cloud
    color = live_color(trial)
    standby = other(color)
    current = trial.manifest["release"]["live_version"]
    warm = cloud.running_tasks(trial.cluster, trial.service_arn(standby))
    previews = sample(trial.preview, cfg.desired * 4)
    standby_versions = {r.version for r in previews if r.status == 200}
    if len(warm) != cfg.desired or len(standby_versions) != 1 or current in standby_versions:
        raise SubmissionFailure(
            f"no warm standby to roll back to: {standby} runs {len(warm)} tasks and preview serves {sorted(standby_versions)}")
    target = standby_versions.pop()

    report, failure = release_under_traffic(trial, target, "deploy-rollback")
    if failure:
        raise failure
    problems = traffic_problems(report, {current, target})
    if not single_changeover(report, current, target):
        problems.append(f"production switched back to {current} after first serving {target}")

    if live_color(trial) != standby:
        problems.append(f"production does not forward to the standby color {standby}")
    now = cloud.running_tasks(trial.cluster, trial.service_arn(standby))
    if now != warm:
        problems.append(f"rollback did not reuse the standby tasks ({len(now - warm)} new, {len(warm - now)} gone)")
    wrong = serving(trial, trial.production, target, standby, cfg.desired * 6)
    if wrong:
        problems.append(f"after rollback production answered {wrong[:4]}, expected {target}/{standby}")
    wrong = serving(trial, trial.preview, current, color, cfg.desired * 6)
    if wrong:
        problems.append(f"after rollback preview answered {wrong[:4]}, expected the warm standby {current}/{color}")
    if len(cloud.wait_running(trial.cluster, trial.service_arn(color), cfg.desired)) != cfg.desired:
        problems.append(f"the {color} color is no longer a warm standby after rollback")

    release = trial.manifest["release"]
    if (release["live_version"], release["live_color"], release["standby_version"], release["last_request"]["outcome"]) \
            != (target, standby, current, "rolled_back"):
        problems.append(f"the manifest release block is {release}")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.instant_rollback", Outcome.PASS,
        f"rollback to {target} switched production onto the {len(warm)} warm {standby} tasks without starting any",
        details={"requests": report.total},
    )


@obligation("lifecycle.drift_restored")
def test_drift_restored(trial: TrialContext) -> CheckResult:
    """Out-of-band listener and capacity changes are put back to the recorded state."""
    cfg, cloud = trial.config, trial.cloud
    color = live_color(trial)
    standby = other(color)
    live_version = trial.manifest["release"]["live_version"]
    standby_version = trial.manifest["release"]["standby_version"]
    live_tasks = cloud.running_tasks(trial.cluster, trial.service_arn(color))
    warm = cloud.running_tasks(trial.cluster, trial.service_arn(standby))
    if not standby_version or len(warm) != cfg.desired:
        raise SubmissionFailure(
            f"no warm standby to drift: {standby} runs {len(warm)} tasks, manifest standby is {standby_version}")

    # The drift, as an operator might cause it: swap the listeners so
    # production serves the standby color, and scale the standby color down.
    edge = trial.manifest["edge"]
    cloud.elbv2.modify_listener(ListenerArn=edge["production_listener_arn"], DefaultActions=[
        {"Type": "forward", "TargetGroupArn": trial.target_group(standby)}])
    cloud.elbv2.modify_listener(ListenerArn=edge["preview_listener_arn"], DefaultActions=[
        {"Type": "forward", "TargetGroupArn": trial.target_group(color)}])
    cloud.ecs.update_service(cluster=trial.cluster, service=trial.service_arn(standby), desiredCount=1)
    if len(cloud.wait_running(trial.cluster, trial.service_arn(standby), 1, timeout=90)) != 1:
        raise HarnessError(f"could not scale {standby} to 1 task to create the drift")
    drifted = [r.version for r in sample(trial.production, cfg.desired * 3) if r.status == 200]
    if not drifted or set(drifted) != {standby_version}:
        raise HarnessError(f"the listener drift did not take effect: production answered {drifted}")

    report, failure = release_under_traffic(trial, None, "deploy-drift")
    if failure:
        raise failure
    problems = traffic_problems(report, {live_version, standby_version})

    routing = trial.routing()
    if routing["production"] != color or routing["preview"] != standby:
        problems.append(f"after deploy production forwards to {routing['production']} and preview to "
                        f"{routing['preview']}; the recorded state is production={color}, preview={standby}")
    wrong = serving(trial, trial.production, live_version, color, cfg.desired * 6)
    if wrong:
        problems.append(f"after deploy production answered {wrong[:4]}, expected {live_version}/{color}")
    if cloud.running_tasks(trial.cluster, trial.service_arn(color)) != live_tasks:
        problems.append(f"restoring the drift stopped or replaced live {color} tasks")
    restored = cloud.wait_running(trial.cluster, trial.service_arn(standby), cfg.desired, timeout=120)
    if len(restored) != cfg.desired:
        problems.append(f"the standby {standby} color runs {len(restored)} tasks, recorded {cfg.desired}")
    wrong = serving(trial, trial.preview, standby_version, standby, cfg.desired * 6)
    if wrong:
        problems.append(f"after deploy preview answered {wrong[:4]}, expected the warm standby {standby_version}/{standby}")
    release = trial.manifest["release"]
    if (release["live_version"], release["live_color"], release["standby_version"], release["last_request"]["outcome"]) \
            != (live_version, color, standby_version, "unchanged"):
        problems.append(f"the manifest release block is {release}")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.drift_restored", Outcome.PASS,
        f"swapped listeners and a scaled-down standby were restored to the recorded state across "
        f"{report.total} production requests with none failing",
        details={"requests": report.total},
    )


@obligation("lifecycle.defective_release_rejected")
def test_defective_release_rejected(trial: TrialContext) -> CheckResult:
    """A release that fails verification never reaches production."""
    cfg, cloud = trial.config, trial.cloud
    bad = cfg.defective_release
    color = live_color(trial)
    live_version = trial.manifest["release"]["live_version"]
    tasks = cloud.running_tasks(trial.cluster, trial.service_arn(color))

    report, failure = release_under_traffic(trial, bad, "deploy-defective")
    served_bad = [s for s in report.samples if s.version == bad]
    after_bad = [r for r in sample(trial.production, cfg.desired * 6) if r.version == bad]
    if served_bad or after_bad:
        raise DefectiveServed(
            f"production was answered by the defective release {bad}: {len(served_bad)} requests during deploy, "
            f"{len(after_bad)} after it")
    if failure:
        raise failure

    problems = traffic_problems(report, {live_version})
    if live_color(trial) != color:
        problems.append(f"production no longer forwards to {color}")
    now = cloud.running_tasks(trial.cluster, trial.service_arn(color))
    if now != tasks:
        problems.append(f"the live {color} tasks changed during a rejected release")
    wrong = serving(trial, trial.production, live_version, color, cfg.desired * 6)
    if wrong:
        problems.append(f"after the rejection production answered {wrong[:4]}, expected {live_version}/{color}")
    idle = cloud.wait_running(trial.cluster, trial.service_arn(other(color)), 0, timeout=90)
    if idle:
        problems.append(f"the rejected candidate color {other(color)} still runs {len(idle)} tasks")

    release = trial.manifest["release"]
    if (release["live_version"], release["live_color"], release["standby_version"], release["last_request"]) \
            != (live_version, color, None, {"version": bad, "outcome": "rejected"}):
        problems.append(f"the manifest release block is {release}")

    # A rejected candidate leaves the standby color at zero. The next healthy
    # release must be staged from that state and proven normally; a controller
    # cannot treat rejection as a terminal/no-op condition.
    # The recovery release is a correct release that is not live: normally the
    # good release; the initial release when the good one is already live.
    good = cfg.good_release if live_version != cfg.good_release else cfg.initial_release
    recovery_report, recovery_failure = release_under_traffic(trial, good, "deploy-recover")
    if recovery_failure:
        raise recovery_failure
    problems.extend(traffic_problems(recovery_report, {live_version, good}))
    if not single_changeover(recovery_report, live_version, good):
        problems.append(f"recovery promotion switched back to {live_version} after serving {good}")
    recovery_color = other(color)
    wrong = serving(trial, trial.production, good, recovery_color, cfg.desired * 6)
    if wrong:
        problems.append(f"after recovery production answered {wrong[:4]}, expected {good}/{recovery_color}")
    wrong = serving(trial, trial.preview, live_version, color, cfg.desired * 6)
    if wrong:
        problems.append(f"after recovery preview answered {wrong[:4]}, expected {live_version}/{color}")
    if cloud.running_tasks(trial.cluster, trial.service_arn(color)) != tasks:
        problems.append(f"recovery replaced the original live {color} tasks")
    recovery = trial.manifest["release"]
    if (recovery["live_version"], recovery["live_color"], recovery["standby_version"],
            recovery["last_request"]) != (good, recovery_color, live_version,
                                             {"version": good, "outcome": "promoted"}):
        problems.append(f"the recovery manifest release block is {recovery}")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.defective_release_rejected", Outcome.PASS,
        f"{bad} was rejected; a later {good} release recovered from the zero-capacity standby and promoted",
        details={"requests": report.total + recovery_report.total},
    )


LOCK_KEY = {"lock_id": {"S": "release-controller"}}
RECORD_KEY = {"lock_id": {"S": "release-record"}}


def _lock_item(client, table: str) -> dict | None:
    return client.get_item(TableName=table, Key=LOCK_KEY, ConsistentRead=True).get("Item")


def _tree_digest(root) -> dict[str, str]:
    """Content digest of every file in the submission copy."""
    digests = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                digests[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                pass
    return digests


@obligation("lifecycle.exclusive_release_lock")
def test_exclusive_release_lock(trial: TrialContext) -> CheckResult:
    """The release lock is taken over when stale, held for the run, released, and respected when live."""
    cfg, cloud = trial.config, trial.cloud
    table = cfg.lock_table
    problems: list[str] = []
    if cloud.table(table) is None:
        raise SubmissionFailure(f"the release lock table {table} does not exist")
    leftover = _lock_item(cloud.ddb, table)
    if leftover:
        raise SubmissionFailure(f"a lock item is still present after every earlier deploy returned: {leftover}")

    # --- Part 1: an expired lease left by a crashed controller is taken over,
    # held for the whole run and released when the run ends.
    color = live_color(trial)
    live_version = trial.manifest["release"]["live_version"]
    live_tasks = cloud.running_tasks(trial.cluster, trial.service_arn(color))
    cloud.ddb.put_item(TableName=table, Item={
        "lock_id": {"S": "release-controller"}, "holder": {"S": "crashed-controller"},
        "lease_expires_at": {"N": str(int(time.time()) - 120)}})

    watcher = boto3.client("dynamodb", region_name=cfg.region, endpoint_url=cfg.endpoint_url,
                           config=BOTO_CONFIG)
    seen: list[tuple[str, int, float]] = []
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            try:
                item = _lock_item(watcher, table)
            except Exception:  # noqa: BLE001 - a missed sample is not a verdict
                item = None
            if item:
                seen.append((item.get("holder", {}).get("S", ""),
                             int(item.get("lease_expires_at", {}).get("N", "0")), time.time()))
            stop.wait(0.5)

    started = time.time()
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        report, failure = release_under_traffic(trial, cfg.defective_release, "deploy-lock-takeover")
    finally:
        stop.set()
        thread.join(timeout=10)
    ended = time.time()
    if failure:
        raise failure
    problems.extend(traffic_problems(report, {live_version}))
    own = [s for s in seen if s[0] and s[0] != "crashed-controller"]
    if not own:
        problems.append("the expired lease was never replaced by a lock held by this run")
    else:
        holders = {holder for holder, _, _ in own}
        if len(holders) > 1:
            problems.append(f"the lock changed holder during one run: {sorted(holders)}")
        acquired = min(expires for _, expires, _ in own) - cfg.lock_lease_seconds
        if not started - 5 <= acquired <= ended + 5:
            problems.append(f"lease_expires_at is not acquisition + lock_lease_seconds "
                            f"({cfg.lock_lease_seconds}); implied acquisition at {acquired:.0f}, run {started:.0f}-{ended:.0f}")
        if own[0][2] - started > 120:
            problems.append("the lock was taken late in the run instead of before acting")
    if _lock_item(cloud.ddb, table):
        problems.append("the run did not release its lock when it ended")
    release = trial.manifest["release"]
    if release["last_request"] != {"version": cfg.defective_release, "outcome": "rejected"} \
            or release["live_version"] != live_version:
        problems.append(f"the takeover run did not reject the defective candidate: {release}")
    if cloud.running_tasks(trial.cluster, trial.service_arn(color)) != live_tasks:
        problems.append("the takeover run replaced live tasks")

    # --- Part 2: a live lease held by someone else refuses the run: exit 75,
    # nothing changed, the lock untouched.
    held = {"lock_id": {"S": "release-controller"}, "holder": {"S": "operator-maintenance"},
            "lease_expires_at": {"N": str(int(time.time()) + 600)}, "note": {"S": "planned maintenance"}}
    cloud.ddb.put_item(TableName=table, Item=held)
    try:
        routing_before = trial.routing()
        tasks_before = {c: cloud.running_tasks(trial.cluster, trial.service_arn(c)) for c in ("blue", "green")}
        tree_before = _tree_digest(trial.config.submission_dir)
        # The standby color is idle after the rejection, so this request would
        # stage a candidate if the lock were ignored.
        status, seconds = deploy_raw(trial.config.submission_dir, trial.config.logs_dir,
                                     "deploy-lock-refused", release=cfg.initial_release)
        if status != 75:
            problems.append(f"while another holder's lease was live, deploy.sh exited {status}, expected 75")
        if seconds > 30:
            problems.append(f"the refused run took {seconds:.0f}s, more than 30s")
        if _tree_digest(trial.config.submission_dir) != tree_before:
            problems.append("the refused run changed files in the submission directory")
        if trial.routing() != routing_before:
            problems.append("the refused run changed listener routing")
        tasks_after = {c: cloud.running_tasks(trial.cluster, trial.service_arn(c)) for c in ("blue", "green")}
        if tasks_after != tasks_before:
            problems.append("the refused run started or stopped tasks")
        if _lock_item(cloud.ddb, table) != held:
            problems.append("the refused run modified or removed another holder's lock")
    finally:
        try:
            cloud.ddb.delete_item(TableName=table, Key=LOCK_KEY)
        except Exception:  # noqa: BLE001
            pass
    trial.reload_manifest()

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.exclusive_release_lock", Outcome.PASS,
        "an expired lease was taken over, held for the run and released; a live lease refused the run with 75 and no change",
        details={"lock_samples": len(own), "requests": report.total},
    )


REFUSAL_SECONDS = 30


def _refusal_snapshot(trial: TrialContext) -> dict:
    """Everything a refused run must leave exactly as it found it."""
    cloud = trial.cloud
    services = {}
    for color in ("blue", "green"):
        service = cloud.service(trial.cluster, trial.service_arn(color)) or {}
        services[color] = (service.get("desiredCount"), service.get("taskDefinition"))
    return {
        "routing": trial.routing(),
        "services": services,
        "tree": _tree_digest(trial.config.submission_dir),
        "lock": _lock_item(cloud.ddb, trial.config.lock_table),
        "record": cloud.ddb.get_item(TableName=trial.config.lock_table, Key=RECORD_KEY,
                                     ConsistentRead=True).get("Item"),
        "manifest": trial.config.manifest_path.read_bytes() if trial.config.manifest_path.is_file() else b"",
    }


def _diff(before: dict, after: dict) -> list[str]:
    names = {"routing": "listener routing", "services": "an ECS service's desired count or task definition",
             "tree": "files in the submission directory", "lock": "the release lock item",
             "record": "the release record",
             "manifest": "manifest.json"}
    return [names[key] for key in before if before[key] != after[key]]


def _refused(trial: TrialContext, label: str, release: str | None, expected: int,
             live_color_: str, live_tasks: set[str]) -> list[str]:
    """Run one request that must be refused; return every way it was not."""
    before = _refusal_snapshot(trial)
    with Traffic(trial.production) as traffic:
        time.sleep(1)
        status, seconds = deploy_raw(trial.config.submission_dir, trial.config.logs_dir, label, release=release)
        time.sleep(1)
    problems = []
    if status != expected:
        problems.append(f"{label}: exited {status}, expected {expected}")
    if seconds > REFUSAL_SECONDS:
        problems.append(f"{label}: took {seconds:.0f}s, more than {REFUSAL_SECONDS}s")
    changed = _diff(before, _refusal_snapshot(trial))
    if changed:
        problems.append(f"{label}: the refused run changed {', '.join(changed)}")
    if trial.cloud.running_tasks(trial.cluster, trial.service_arn(live_color_)) != live_tasks:
        problems.append(f"{label}: the refused run started, stopped or replaced live tasks")
    if traffic.report.failures():
        problems.append(f"{label}: production requests failed: {traffic.report.describe()}")
    return problems


@obligation("lifecycle.refusals_side_effect_free")
def test_refusals_side_effect_free(trial: TrialContext) -> CheckResult:
    """Refused runs are bounded and change and repair nothing; the next run recovers."""
    cfg, cloud = trial.config, trial.cloud
    table = cfg.lock_table
    color = live_color(trial)
    standby = other(color)
    live_version = trial.manifest["release"]["live_version"]
    live_tasks = cloud.running_tasks(trial.cluster, trial.service_arn(color))
    if _lock_item(cloud.ddb, table):
        raise SubmissionFailure("a lock item is still present after every earlier deploy returned")
    versions = {r["version"] for r in cfg.values["releases"]}
    unknown = ["9.9.9", f"v{cfg.good_release}"]
    if versions & set(unknown):
        raise HarnessError(f"an unknown-release probe is a real release: {sorted(versions & set(unknown))}")
    problems: list[str] = []

    # 1. Unknown releases, including an exact-match near miss: 64, nothing changed.
    for index, release in enumerate(unknown):
        problems += _refused(trial, f"deploy-unknown-{index}", release, 64, color, live_tasks)

    foreign = {"lock_id": {"S": "release-controller"}, "holder": {"S": "operator-maintenance"},
               "lease_expires_at": {"N": str(int(time.time()) + 900)}, "note": {"S": "planned maintenance"}}
    try:
        # 2. Precedence: an unknown release is refused with 64 before the lock is
        # looked at; the live foreign lease is left exactly as it was.
        cloud.ddb.put_item(TableName=table, Item=foreign)
        problems += _refused(trial, "deploy-unknown-under-lease", unknown[0], 64, color, live_tasks)

        # 3. Drift under a live foreign lease: the refused run repairs nothing.
        recorded_standby = 0 if trial.manifest["release"]["standby_version"] is None else cfg.desired
        drifted_count = 1 if recorded_standby == 0 else 0
        cloud.ecs.update_service(cluster=trial.cluster, service=trial.service_arn(standby),
                                 desiredCount=drifted_count)
        cloud.elbv2.modify_listener(ListenerArn=trial.manifest["edge"]["preview_listener_arn"], DefaultActions=[
            {"Type": "forward", "TargetGroupArn": trial.target_group(color)}])
        if len(cloud.wait_running(trial.cluster, trial.service_arn(standby), drifted_count, timeout=90)) != drifted_count:
            raise HarnessError(f"could not drift {standby} to {drifted_count} task(s)")
        if trial.routing()["preview"] != color:
            raise HarnessError("the preview listener drift did not take effect")
        problems += _refused(trial, "deploy-drift-under-lease", None, 75, color, live_tasks)
        drifted = cloud.service(trial.cluster, trial.service_arn(standby)) or {}
        if drifted.get("desiredCount") != drifted_count or trial.routing()["preview"] != color:
            problems.append("the refused run repaired drift it must leave alone")
    finally:
        try:
            cloud.ddb.delete_item(TableName=table, Key=LOCK_KEY)
        except Exception:  # noqa: BLE001
            pass

    # 4. Recovery: with the lease gone, the next run repairs the drift as if
    # the refused runs never happened.
    report, failure = release_under_traffic(trial, None, "deploy-after-refusals")
    if failure:
        raise failure
    problems.extend(traffic_problems(report, {live_version}))
    routing = trial.routing()
    if routing["production"] != color or routing["preview"] != standby:
        problems.append(f"after recovery production forwards to {routing['production']} and preview to "
                        f"{routing['preview']}; recorded production={color}, preview={standby}")
    restored = cloud.wait_running(trial.cluster, trial.service_arn(standby), recorded_standby, timeout=120)
    if len(restored) != recorded_standby:
        problems.append(f"after recovery {standby} runs {len(restored)} tasks, recorded {recorded_standby}")
    if cloud.running_tasks(trial.cluster, trial.service_arn(color)) != live_tasks:
        problems.append(f"recovery replaced live {color} tasks")
    release = trial.manifest["release"]
    if release["live_version"] != live_version or release["last_request"]["outcome"] != "unchanged":
        problems.append(f"after recovery the manifest release block is {release}")
    if _lock_item(cloud.ddb, table):
        problems.append("the recovery run did not release its lock")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.refusals_side_effect_free", Outcome.PASS,
        "unknown releases (64, checked before the lock) and a live foreign lease (75) were refused within "
        f"{REFUSAL_SECONDS}s with nothing changed or repaired; the next run restored the recorded state",
        details={"requests": report.total},
    )


def _record(trial: TrialContext) -> dict | None:
    """The release record, decoded; None when absent."""
    item = trial.cloud.ddb.get_item(TableName=trial.config.lock_table, Key=RECORD_KEY,
                                    ConsistentRead=True).get("Item")
    if not item:
        return None
    out = {}
    for key, value in item.items():
        if "L" in value:
            # verified: order and duplicates are not significant
            out[key] = sorted({entry["S"] for entry in value["L"] if isinstance(entry, dict) and "S" in entry})
        elif "S" in value:
            out[key] = value["S"]
        elif "N" in value:
            try:
                out[key] = int(value["N"])
            except ValueError:
                out[key] = value["N"]
    return out


def _record_problems(trial: TrialContext, label: str) -> tuple[dict, list[str]]:
    """Check the release record against what is actually deployed."""
    cfg, cloud = trial.config, trial.cloud
    record = _record(trial)
    if record is None:
        return {}, [f"{label}: there is no release record item (lock_id release-record)"]
    problems = []
    required = ("generation", "live_color", "blue_release", "green_release", "blue_count", "green_count",
                "last_request_version", "last_request_outcome", "verified")
    missing = [key for key in required if key not in record]
    if missing:
        return record, [f"{label}: the release record lacks {missing}"]
    if not isinstance(record["generation"], int) or record["generation"] < 1:
        problems.append(f"{label}: record generation is {record['generation']!r}")
    if not isinstance(record["verified"], list) or not record["verified"]:
        problems.append(f"{label}: record verified is {record['verified']!r}, expected a non-empty list of releases")
    elif record.get(f"{record['live_color']}_release") not in record["verified"]:
        problems.append(f"{label}: the live release {record.get(record['live_color'] + '_release')} is not in "
                        f"verified {record['verified']}")
    routing = trial.routing()
    if record["live_color"] != routing["production"]:
        problems.append(f"{label}: record live_color {record['live_color']} but production forwards to "
                        f"{routing['production']}")
    for color in ("blue", "green"):
        count = record[f"{color}_count"]
        running = cloud.wait_running(trial.cluster, trial.service_arn(color), count, timeout=60) \
            if isinstance(count, int) else set()
        if not isinstance(count, int) or len(running) != count:
            problems.append(f"{label}: record {color}_count {count!r} but {color} runs {len(running)} tasks")
            continue
        if count:
            api = trial.production if color == routing["production"] else trial.preview
            versions = {r.version for r in sample(api, cfg.desired * 3) if r.status == 200}
            if versions != {record[f"{color}_release"]}:
                problems.append(f"{label}: record {color}_release {record[f'{color}_release']} but {color} "
                                f"serves {sorted(versions)}")
    last = trial.manifest["release"]["last_request"]
    if (record["last_request_version"], record["last_request_outcome"]) != (last["version"], last["outcome"]):
        problems.append(f"{label}: record last request {record['last_request_version']}/"
                        f"{record['last_request_outcome']} but manifest says {last}")
    return record, problems


def _fresh_worker(trial: TrialContext) -> None:
    """Replace the submission copy with a fresh one, keeping only Terraform state."""
    work = trial.config.submission_dir
    state_path = work / "infra" / "terraform.tfstate"
    if not state_path.is_file():
        raise SubmissionFailure("infra/terraform.tfstate does not exist; Terraform state must be local in infra/")
    state = state_path.read_bytes()
    shutil.rmtree(work)
    _writable_copy(trial.config.source_submission, work)
    (work / "infra").mkdir(parents=True, exist_ok=True)
    (work / "infra" / "terraform.tfstate").write_bytes(state)


@obligation("lifecycle.state_recovered_from_record")
def test_state_recovered_from_record(trial: TrialContext) -> CheckResult:
    """The cloud release record is accurate and enough to continue on a fresh worker."""
    cfg, cloud = trial.config, trial.cloud
    problems: list[str] = []
    record, found = _record_problems(trial, "before")
    if found:
        raise SubmissionFailure("; ".join(found))
    generation = record["generation"]

    # 1. Build a warm standby: promote a known-good release that is not live.
    color = live_color(trial)
    standby = other(color)
    live_version = trial.manifest["release"]["live_version"]
    target = cfg.initial_release if live_version != cfg.initial_release else cfg.good_release
    old_live_tasks = cloud.running_tasks(trial.cluster, trial.service_arn(color))
    report, failure = release_under_traffic(trial, target, "deploy-record-promote")
    if failure:
        raise failure
    problems += traffic_problems(report, {live_version, target})
    if trial.manifest["release"]["last_request"] != {"version": target, "outcome": "promoted"}:
        raise SubmissionFailure(f"promoting {target} did not succeed: {trial.manifest['release']}")
    record, found = _record_problems(trial, "after promotion")
    problems += found
    if record.get("generation") != generation + 1:
        problems.append(f"promotion moved the record generation from {generation} to {record.get('generation')}, "
                        "expected exactly one step")
    new_live_tasks = cloud.running_tasks(trial.cluster, trial.service_arn(standby))
    warm = cloud.running_tasks(trial.cluster, trial.service_arn(color))
    if warm != old_live_tasks:
        problems.append(f"the previously live {color} tasks were not kept warm by the promotion")

    # 2. A fresh worker: everything but Terraform state is gone.
    _fresh_worker(trial)

    # 3. No release requested: nothing serving changes, no task starts or stops.
    report, failure = release_under_traffic(trial, None, "deploy-record-fresh-worker")
    if failure:
        raise failure
    problems += traffic_problems(report, {target})
    if trial.manifest["release"]["last_request"] != {"version": target, "outcome": "unchanged"}:
        problems.append(f"on a fresh worker a no-release run recorded {trial.manifest['release']['last_request']}, "
                        f"expected {target}/unchanged")
    routing = trial.routing()
    if (routing["production"], routing["preview"]) != (standby, color):
        problems.append(f"on a fresh worker production forwards to {routing['production']} and preview to "
                        f"{routing['preview']}, expected {standby} and {color}")
    if cloud.running_tasks(trial.cluster, trial.service_arn(standby)) != new_live_tasks:
        problems.append("on a fresh worker the live tasks were started, stopped or replaced")
    if cloud.running_tasks(trial.cluster, trial.service_arn(color)) != warm:
        problems.append("on a fresh worker the warm standby tasks were started, stopped or replaced")
    record, found = _record_problems(trial, "fresh worker")
    problems += found
    if record.get("generation") != generation + 1:
        problems.append(f"a run that changed nothing moved the record generation to {record.get('generation')}, "
                        f"expected {generation + 1}")

    # 4. The warm standby is still a rollback target, found from the record.
    report, failure = release_under_traffic(trial, live_version, "deploy-record-rollback")
    if failure:
        raise failure
    problems += traffic_problems(report, {target, live_version})
    if trial.manifest["release"]["last_request"] != {"version": live_version, "outcome": "rolled_back"}:
        problems.append(f"after a fresh worker, requesting the warm standby {live_version} recorded "
                        f"{trial.manifest['release']['last_request']}, expected rolled_back")
    if trial.routing()["production"] != color:
        problems.append(f"the rollback did not return production to {color}")
    if cloud.running_tasks(trial.cluster, trial.service_arn(color)) != warm:
        problems.append("the rollback did not reuse exactly the warm standby tasks")
    record, found = _record_problems(trial, "after rollback")
    problems += found
    if record.get("generation") != generation + 2:
        problems.append(f"the rollback moved the record generation to {record.get('generation')}, "
                        f"expected {generation + 2}")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.state_recovered_from_record", Outcome.PASS,
        f"the release record tracked a promotion (generation {generation}->{generation + 1}); on a fresh worker a "
        f"no-release run changed nothing and the warm standby {live_version} was rolled back to "
        f"(generation {generation + 2})",
    )


RECORD_FIELDS = ("generation", "live_color", "blue_release", "green_release", "blue_count", "green_count",
                 "last_request_version", "last_request_outcome", "verified")
CRASH_STATUSES = {137, -9}  # exit 137, or killed by SIGKILL


def _documented(record: dict | None) -> dict:
    return {key: (record or {}).get(key) for key in RECORD_FIELDS}


def _crash_run(trial: TrialContext, label: str, release: str, point: str) -> tuple[int, TrafficReport]:
    """Run deploy.sh asked to stop at a crash point, under production traffic."""
    with Traffic(trial.production) as traffic:
        time.sleep(2)
        status, _seconds = deploy_raw(trial.config.submission_dir, trial.config.logs_dir, label,
                                      release=release, fault_point=point)
        time.sleep(2)
    trial.facts.setdefault("quote_ids", []).extend(traffic.report.quote_ids())
    return status, traffic.report


def _crash_leftovers(trial: TrialContext, label: str, status: int, record_before: dict,
                     manifest_before: bytes) -> list[str]:
    """What a crashed run must leave: its live lock, the old record, the old manifest."""
    cfg = trial.config
    problems = []
    if status not in CRASH_STATUSES:
        problems.append(f"{label}: exited {status}, expected 137 at the crash point")
    lock = _lock_item(trial.cloud.ddb, cfg.lock_table)
    if not lock:
        problems.append(f"{label}: the crashed run removed its release lock item")
    else:
        try:
            expires = int(lock.get("lease_expires_at", {}).get("N", "0"))
        except ValueError:
            expires = 0
        if expires <= int(time.time()):
            problems.append(f"{label}: the crashed run left a lock whose lease is not live ({lock})")
    if _documented(_record(trial)) != record_before:
        problems.append(f"{label}: the crashed run changed the release record to {_documented(_record(trial))}, "
                        f"expected it unchanged from {record_before}")
    manifest = cfg.manifest_path.read_bytes() if cfg.manifest_path.is_file() else b""
    if manifest != manifest_before:
        problems.append(f"{label}: the crashed run rewrote manifest.json")
    return problems


def _expire_lease(trial: TrialContext) -> None:
    """The operator ends a crashed holder's lease (crash-recovery.md, Recovering)."""
    try:
        trial.cloud.ddb.update_item(
            TableName=trial.config.lock_table, Key=LOCK_KEY,
            UpdateExpression="SET lease_expires_at = :past",
            ConditionExpression="attribute_exists(lock_id)",
            ExpressionAttributeValues={":past": {"N": str(int(time.time()) - 60)}})
    except trial.cloud.ddb.exceptions.ConditionalCheckFailedException:
        pass  # no lock item left behind: already reported


def _wait_preview(trial: TrialContext, version: str, color: str, count: int, timeout: int = 60) -> list:
    """Sample preview until `count` consecutive answers are version/color, or time runs out."""
    deadline = time.monotonic() + timeout
    while True:
        wrong = serving(trial, trial.preview, version, color, count)
        if not wrong or time.monotonic() >= deadline:
            return wrong
        time.sleep(3)


def _after_recovery(trial: TrialContext, label: str, report: TrafficReport, failure: BaseException | None,
                    live: str, live_version: str, standby_version: str, generation: int,
                    unchanged_tasks: dict[str, set[str]], allowed: set[str]) -> list[str]:
    cfg, cloud = trial.config, trial.cloud
    if failure:
        return [f"{label}: {failure}"]
    standby = other(live)
    problems = [f"{label}: {p}" for p in traffic_problems(report, allowed)]
    routing = trial.routing()
    if (routing["production"], routing["preview"]) != (live, standby):
        problems.append(f"{label}: production forwards to {routing['production']} and preview to "
                        f"{routing['preview']}; the record says {live} and {standby}")
    for color, tasks in unchanged_tasks.items():
        if cloud.running_tasks(trial.cluster, trial.service_arn(color)) != tasks:
            problems.append(f"{label}: {color} tasks were started, stopped or replaced although {color} "
                            "already ran its recorded release")
    if len(cloud.wait_running(trial.cluster, trial.service_arn(standby), cfg.desired, timeout=120)) != cfg.desired:
        problems.append(f"{label}: the standby {standby} does not run {cfg.desired} tasks")
    wrong = _wait_preview(trial, standby_version, standby, cfg.desired * 4, timeout=30)
    if wrong:
        problems.append(f"{label}: preview answered {wrong[:4]}, expected the recorded standby "
                        f"{standby_version}/{standby}")
    wrong = serving(trial, trial.production, live_version, live, cfg.desired * 4)
    if wrong:
        problems.append(f"{label}: production answered {wrong[:4]}, expected {live_version}/{live}")
    if trial.manifest["release"]["last_request"] != {"version": live_version, "outcome": "unchanged"}:
        problems.append(f"{label}: recorded {trial.manifest['release']['last_request']}, "
                        f"expected {live_version}/unchanged")
    record, found = _record_problems(trial, label)
    problems += found
    if record.get("generation") != generation:
        problems.append(f"{label}: the recovery moved the record generation from {generation} to "
                        f"{record.get('generation')}; the recorded state did not change")
    if _lock_item(cloud.ddb, cfg.lock_table):
        problems.append(f"{label}: the recovery run did not release its lock")
    return problems


@obligation("lifecycle.crash_points_recovered")
def test_crash_points_recovered(trial: TrialContext) -> CheckResult:
    """A controller killed mid-release leaves its lock; the next run restores the record."""
    cfg, cloud = trial.config, trial.cloud
    record, found = _record_problems(trial, "before")
    if found:
        raise SubmissionFailure("; ".join(found))
    if _lock_item(cloud.ddb, cfg.lock_table):
        raise SubmissionFailure("a lock item is still present after every earlier deploy returned")
    live = live_color(trial)
    standby = other(live)
    live_version = trial.manifest["release"]["live_version"]
    standby_version = trial.manifest["release"]["standby_version"]
    live_tasks = cloud.running_tasks(trial.cluster, trial.service_arn(live))
    warm = cloud.running_tasks(trial.cluster, trial.service_arn(standby))
    if not standby_version or len(warm) != cfg.desired:
        raise SubmissionFailure(f"no warm standby before the crash checks: {standby} runs {len(warm)} tasks, "
                                f"manifest standby {standby_version}")
    defective = cfg.defective_release
    if defective in (live_version, standby_version):
        raise HarnessError(f"the defective release {defective} is already deployed")
    generation = record["generation"]
    before = _documented(record)
    problems: list[str] = []

    # 1. Crash at `staged`: the defective candidate is warm in the standby
    # color behind preview, its self-test not yet acted on.
    manifest = cfg.manifest_path.read_bytes() if cfg.manifest_path.is_file() else b""
    status, report = _crash_run(trial, "deploy-crash-staged", defective, "staged")
    problems += [f"crash at staged: {p}" for p in traffic_problems(report, {live_version})]
    problems += _crash_leftovers(trial, "crash at staged", status, before, manifest)
    routing = trial.routing()
    if (routing["production"], routing["preview"]) != (live, standby):
        problems.append(f"crash at staged: production forwards to {routing['production']} and preview to "
                        f"{routing['preview']}, expected {live} and {standby}")
    if cloud.running_tasks(trial.cluster, trial.service_arn(live)) != live_tasks:
        problems.append("crash at staged: the live tasks were started, stopped or replaced")
    if len(cloud.wait_running(trial.cluster, trial.service_arn(standby), cfg.desired, timeout=60)) != cfg.desired:
        problems.append(f"crash at staged: the candidate color {standby} does not run {cfg.desired} tasks")
    wrong = _wait_preview(trial, defective, standby, cfg.desired * 4)
    if wrong:
        problems.append(f"crash at staged: preview answered {wrong[:4]}, expected the warm candidate "
                        f"{defective}/{standby}")
    if _lock_item(cloud.ddb, cfg.lock_table):
        # The crashed holder's live lease is respected like any other.
        problems += _refused(trial, "deploy-crash-lease-live", None, 75, live, live_tasks)
    _expire_lease(trial)

    # 2. The next run takes over and restores the recorded standby release.
    report, failure = release_under_traffic(trial, None, "deploy-crash-staged-recover")
    problems += _after_recovery(trial, "recovery after staged", report, failure, live, live_version,
                                standby_version, generation, {live: live_tasks}, {live_version})
    if problems:
        raise SubmissionFailure("; ".join(problems))
    warm = cloud.running_tasks(trial.cluster, trial.service_arn(standby))
    record = _record(trial) or {}
    before = _documented(record)

    # 3. Crash at `switched`: a rollback has swapped the listeners.
    manifest = cfg.manifest_path.read_bytes() if cfg.manifest_path.is_file() else b""
    status, report = _crash_run(trial, "deploy-crash-switched", standby_version, "switched")
    problems += [f"crash at switched: {p}" for p in traffic_problems(report, {live_version, standby_version})]
    problems += _crash_leftovers(trial, "crash at switched", status, before, manifest)
    routing = trial.routing()
    if (routing["production"], routing["preview"]) != (standby, live):
        problems.append(f"crash at switched: production forwards to {routing['production']} and preview to "
                        f"{routing['preview']}, expected the rollback's {standby} and {live}")
    if cloud.running_tasks(trial.cluster, trial.service_arn(standby)) != warm:
        problems.append("crash at switched: the rollback did not reuse exactly the warm standby tasks")
    if cloud.running_tasks(trial.cluster, trial.service_arn(live)) != live_tasks:
        problems.append(f"crash at switched: the {live} tasks were started, stopped or replaced")
    _expire_lease(trial)

    # 4. The next run puts the recorded routing back; both colors already
    # run their recorded releases, so no task starts or stops.
    report, failure = release_under_traffic(trial, None, "deploy-crash-switched-recover")
    problems += _after_recovery(trial, "recovery after switched", report, failure, live, live_version,
                                standby_version, generation, {live: live_tasks, standby: warm},
                                {live_version, standby_version})

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.crash_points_recovered", Outcome.PASS,
        f"runs killed at 'staged' ({defective} warm in {standby}) and at 'switched' (rollback listeners swapped) "
        f"left their lock and the record (generation {generation}); after the leases were expired the next runs "
        f"restored {live_version}/{live} live and {standby_version}/{standby} warm with no failed request, "
        "outcome unchanged and the generation kept",
    )


# ---- operator-edited release record (release-record.md, rules 5 and Validity) ----

def _raw_record(trial: TrialContext) -> dict:
    item = trial.cloud.ddb.get_item(TableName=trial.config.lock_table, Key=RECORD_KEY,
                                    ConsistentRead=True).get("Item")
    if not item:
        raise SubmissionFailure("there is no release record item (lock_id release-record)")
    return item


def _encode(value) -> dict:
    if isinstance(value, bool):
        raise HarnessError("booleans are not record values")
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, list):
        return {"L": [{"S": str(entry)} for entry in value]}
    return {"S": str(value)}


def _operator_edit(trial: TrialContext, **changes) -> int:
    """Edit the record the way an operator would: change values, generation + 1."""
    if _lock_item(trial.cloud.ddb, trial.config.lock_table):
        raise SubmissionFailure("a lock item is still present after every earlier deploy returned")
    item = _raw_record(trial)
    try:
        generation = int(item["generation"]["N"]) + 1
    except (KeyError, ValueError) as exc:
        raise SubmissionFailure(f"the release record has no usable generation: {item.get('generation')}") from exc
    for key, value in changes.items():
        item[key] = _encode(value)
    item["generation"] = {"N": str(generation)}
    trial.cloud.ddb.put_item(TableName=trial.config.lock_table, Item=item)
    return generation


def _directive_run(trial: TrialContext, label: str, release: str | None, generation: int | None,
                   allowed: set[str], unchanged: dict[str, set[str]]) -> tuple[list[str], dict]:
    """Run deploy under traffic and check what every directive run must hold."""
    report, failure = release_under_traffic(trial, release, label)
    if failure:
        return [f"{label}: {failure}"], {}
    problems = [f"{label}: {p}" for p in traffic_problems(report, allowed)]
    for color, tasks in unchanged.items():
        if trial.cloud.running_tasks(trial.cluster, trial.service_arn(color)) != tasks:
            problems.append(f"{label}: {color} tasks were started, stopped or replaced although {color} already "
                            "ran its recorded release at its recorded count")
    record, found = _record_problems(trial, label)
    problems += found
    if generation is not None and record.get("generation") != generation:
        problems.append(f"{label}: the record generation is {record.get('generation')}, expected {generation}")
    if _lock_item(trial.cloud.ddb, trial.config.lock_table):
        problems.append(f"{label}: the run did not release its lock")
    return problems, record


def _expect_release(trial: TrialContext, label: str, live: str, live_version: str,
                    standby_version: str | None, last: dict) -> list[str]:
    release = trial.manifest["release"]
    want = (live_version, live, standby_version, last)
    have = (release["live_version"], release["live_color"], release["standby_version"], release["last_request"])
    return [] if have == want else [f"{label}: the manifest release block is {release}, expected "
                                    f"live {live_version}/{live}, standby {standby_version}, last request {last}"]


@obligation("lifecycle.operator_directives_applied")
def test_operator_directives_applied(trial: TrialContext) -> CheckResult:
    """An operator's edits to the record are carried out with zero downtime."""
    cfg, cloud = trial.config, trial.cloud
    record, found = _record_problems(trial, "before")
    if found:
        raise SubmissionFailure("; ".join(found))
    live = live_color(trial)
    standby = other(live)
    live_version = trial.manifest["release"]["live_version"]
    standby_version = trial.manifest["release"]["standby_version"]
    live_tasks = cloud.running_tasks(trial.cluster, trial.service_arn(live))
    warm = cloud.running_tasks(trial.cluster, trial.service_arn(standby))
    if not standby_version or len(warm) != cfg.desired or standby_version not in record["verified"]:
        raise SubmissionFailure(f"no verified warm standby before the operator checks: {standby} runs {len(warm)} "
                                f"tasks of {standby_version}, verified {record['verified']}")

    # 1. The operator points live_color at the warm standby: the next run
    # switches listeners onto exactly those tasks; nothing starts or stops.
    generation = _operator_edit(trial, live_color=standby)
    problems, _ = _directive_run(trial, "deploy-operator-swap", None, generation,
                                 {live_version, standby_version}, {standby: warm, live: live_tasks})
    routing = trial.routing()
    if (routing["production"], routing["preview"]) != (standby, live):
        problems.append(f"operator swap: production forwards to {routing['production']} and preview to "
                        f"{routing['preview']}, the record says {standby} and {live}")
    problems += _expect_release(trial, "operator swap", standby, standby_version, live_version,
                                {"version": standby_version, "outcome": "unchanged"})
    wrong = serving(trial, trial.production, standby_version, standby, cfg.desired * 4)
    if wrong:
        problems.append(f"operator swap: production answered {wrong[:4]}, expected {standby_version}/{standby}")
    if problems:
        raise SubmissionFailure("; ".join(problems))
    new_live, new_standby = standby, live  # roles after the swap

    # 2. The operator retires the standby: it scales to zero, live untouched.
    generation = _operator_edit(trial, **{f"{new_standby}_count": 0})
    problems, _ = _directive_run(trial, "deploy-operator-retire", None, generation, {standby_version},
                                 {new_live: warm})
    if cloud.wait_running(trial.cluster, trial.service_arn(new_standby), 0, timeout=120):
        problems.append(f"operator retire: {new_standby} still runs tasks; the record says 0")
    problems += _expect_release(trial, "operator retire", new_live, standby_version, None,
                                {"version": standby_version, "outcome": "unchanged"})

    # 3. The operator brings the standby back with a verified release.
    generation = _operator_edit(trial, **{f"{new_standby}_count": cfg.desired,
                                          f"{new_standby}_release": live_version})
    more, _ = _directive_run(trial, "deploy-operator-provision", None, generation, {standby_version},
                             {new_live: warm})
    problems += more
    if len(cloud.wait_running(trial.cluster, trial.service_arn(new_standby), cfg.desired, timeout=120)) != cfg.desired:
        problems.append(f"operator provision: {new_standby} does not run {cfg.desired} tasks")
    wrong = _wait_preview(trial, live_version, new_standby, cfg.desired * 4, timeout=30)
    if wrong:
        problems.append(f"operator provision: preview answered {wrong[:4]}, expected {live_version}/{new_standby}")
    problems += _expect_release(trial, "operator provision", new_live, standby_version, live_version,
                                {"version": standby_version, "outcome": "unchanged"})

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.operator_directives_applied", Outcome.PASS,
        f"operator edits were carried out from the record: production moved onto the warm {standby} tasks with "
        f"nothing started, the {live} standby was retired and brought back with {live_version}, every run "
        "unchanged with the operator's generation kept",
    )


@obligation("lifecycle.unverified_standby_proven")
def test_unverified_standby_proven(trial: TrialContext) -> CheckResult:
    """A standby release that is not in verified is a candidate, never a rollback target."""
    cfg, cloud = trial.config, trial.cloud
    record, found = _record_problems(trial, "before")
    if found:
        raise SubmissionFailure("; ".join(found))
    live = live_color(trial)
    standby = other(live)
    live_version = trial.manifest["release"]["live_version"]
    standby_version = trial.manifest["release"]["standby_version"]
    bad = cfg.defective_release
    live_tasks = cloud.running_tasks(trial.cluster, trial.service_arn(live))
    if not standby_version or bad in (live_version, standby_version) or bad in record["verified"]:
        raise SubmissionFailure(f"unexpected state before the check: live {live_version}, standby "
                                f"{standby_version}, verified {record['verified']}")
    problems: list[str] = []

    def defective_in_production(report) -> list[str]:
        served = [s for s in report.samples if s.version == bad]
        after = [r for r in sample(trial.production, cfg.desired * 4) if r.version == bad]
        return [f"production was answered by the unverified {bad}: {len(served)} requests during deploy, "
                f"{len(after)} after"] if served or after else []

    # 1. The operator places the defective release in the standby. The run
    # brings the standby to it; production is untouched; it is not verified.
    generation = _operator_edit(trial, **{f"{standby}_release": bad, f"{standby}_count": cfg.desired})
    report, failure = release_under_traffic(trial, None, "deploy-operator-stage-unverified")
    problems += defective_in_production(report)
    if failure:
        raise SubmissionFailure("; ".join(problems + [str(failure)]))
    problems += [f"operator staging: {p}" for p in traffic_problems(report, {live_version})]
    if cloud.running_tasks(trial.cluster, trial.service_arn(live)) != live_tasks:
        problems.append("operator staging: the live tasks were started, stopped or replaced")
    wrong = _wait_preview(trial, bad, standby, cfg.desired * 4, timeout=60)
    if wrong:
        problems.append(f"operator staging: preview answered {wrong[:4]}, expected the operator's {bad}/{standby}")
    record, found = _record_problems(trial, "operator staging")
    problems += found
    if bad in record.get("verified", []):
        problems.append(f"operator staging: {bad} was added to verified without passing its self-test")
    if record.get("generation") != generation:
        problems.append(f"operator staging: the generation is {record.get('generation')}, expected {generation}")
    if problems:
        raise SubmissionFailure("; ".join(problems))

    # 2. Requesting it is not a rollback: it is self-tested and rejected.
    report, failure = release_under_traffic(trial, bad, "deploy-unverified-request")
    problems += defective_in_production(report)
    if failure:
        raise SubmissionFailure("; ".join(problems + [str(failure)]))
    problems += [f"unverified request: {p}" for p in traffic_problems(report, {live_version})]
    if live_color(trial) != live or cloud.running_tasks(trial.cluster, trial.service_arn(live)) != live_tasks:
        problems.append("unverified request: production or its tasks changed")
    if cloud.wait_running(trial.cluster, trial.service_arn(standby), 0, timeout=120):
        problems.append(f"unverified request: the rejected {standby} still runs tasks")
    problems += _expect_release(trial, "unverified request", live, live_version, None,
                                {"version": bad, "outcome": "rejected"})
    record, found = _record_problems(trial, "unverified request")
    problems += found
    if bad in record.get("verified", []):
        problems.append(f"unverified request: {bad} is in verified after failing its self-test")
    if record.get("generation") != generation + 1:
        problems.append(f"unverified request: the generation is {record.get('generation')}, expected {generation + 1}")
    if problems:
        raise SubmissionFailure("; ".join(problems))

    # 3. The operator revokes a good release and places it in the standby.
    revoked = [r for r in record["verified"] if r != standby_version]
    generation = _operator_edit(trial, verified=revoked, **{f"{standby}_release": standby_version,
                                                            f"{standby}_count": cfg.desired})
    more, record = _directive_run(trial, "deploy-operator-stage-revoked", None, generation, {live_version},
                                  {live: live_tasks})
    problems += more
    wrong = _wait_preview(trial, standby_version, standby, cfg.desired * 4, timeout=60)
    if wrong:
        problems.append(f"revoked staging: preview answered {wrong[:4]}, expected {standby_version}/{standby}")
    if standby_version in record.get("verified", []):
        problems.append(f"revoked staging: {standby_version} is back in verified although it was not proven again")
    if problems:
        raise SubmissionFailure("; ".join(problems))

    # 4. Requesting it proves it first: outcome promoted, verified again.
    report, failure = release_under_traffic(trial, standby_version, "deploy-revoked-request")
    if failure:
        raise failure
    problems += [f"revoked request: {p}" for p in traffic_problems(report, {live_version, standby_version})]
    if not single_changeover(report, live_version, standby_version):
        problems.append(f"revoked request: production switched back to {live_version} after serving {standby_version}")
    problems += _expect_release(trial, "revoked request", standby, standby_version, live_version,
                                {"version": standby_version, "outcome": "promoted"})
    wrong = serving(trial, trial.production, standby_version, standby, cfg.desired * 4)
    if wrong:
        problems.append(f"revoked request: production answered {wrong[:4]}, expected {standby_version}/{standby}")
    if cloud.running_tasks(trial.cluster, trial.service_arn(live)) != live_tasks:
        problems.append(f"revoked request: the previously live {live} tasks were not kept as the warm standby")
    record, found = _record_problems(trial, "revoked request")
    problems += found
    if standby_version not in record.get("verified", []):
        problems.append(f"revoked request: {standby_version} passed its self-test but is not in verified")
    if record.get("generation") != generation + 1:
        problems.append(f"revoked request: the generation is {record.get('generation')}, expected {generation + 1}")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.unverified_standby_proven", Outcome.PASS,
        f"an operator-placed {bad} standby was self-tested and rejected instead of rolled back to; a revoked "
        f"{standby_version} standby was proven again and promoted (outcome promoted, verified restored)",
    )


@obligation("lifecycle.undeployable_record_refused")
def test_undeployable_record_refused(trial: TrialContext) -> CheckResult:
    """A record that is not a deployable state is refused with 65, changing nothing."""
    cfg, cloud = trial.config, trial.cloud
    record, found = _record_problems(trial, "before")
    if found:
        raise SubmissionFailure("; ".join(found))
    original = _raw_record(trial)
    live = live_color(trial)
    standby = other(live)
    live_version = record[f"{live}_release"]
    live_tasks = cloud.running_tasks(trial.cluster, trial.service_arn(live))
    standby_tasks = cloud.running_tasks(trial.cluster, trial.service_arn(standby))
    generation = record["generation"]
    without_live = [r for r in record["verified"] if r != live_version]
    variants = [
        ("deploy-record-live-idle", None, {f"{live}_count": 0}),
        ("deploy-record-odd-count", None, {f"{standby}_count": cfg.desired + 1}),
        ("deploy-record-live-unverified", None, {"verified": without_live or ["9.9.9"]}),
        ("deploy-record-unknown-release", cfg.initial_release,
         {f"{standby}_release": "9.9.9", f"{standby}_count": cfg.desired}),
    ]
    problems: list[str] = []
    try:
        for label, release, changes in variants:
            item = dict(original)
            for key, value in changes.items():
                item[key] = _encode(value)
            item["generation"] = {"N": str(generation + 1)}
            cloud.ddb.put_item(TableName=cfg.lock_table, Item=item)
            problems += _refused(trial, label, release, 65, live, live_tasks)
            if cloud.running_tasks(trial.cluster, trial.service_arn(standby)) != standby_tasks:
                problems.append(f"{label}: the refused run started, stopped or replaced standby tasks")
    finally:
        cloud.ddb.put_item(TableName=cfg.lock_table, Item=original)

    # The operator puts the record right: the next run proceeds as if nothing happened.
    more, _ = _directive_run(trial, "deploy-record-fixed", None, generation,
                             {live_version}, {live: live_tasks, standby: standby_tasks})
    problems += more
    if trial.manifest["release"]["last_request"] != {"version": live_version, "outcome": "unchanged"}:
        problems.append(f"after the record was fixed the run recorded {trial.manifest['release']['last_request']}")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.undeployable_record_refused", Outcome.PASS,
        f"four undeployable records were each refused with 65 within {REFUSAL_SECONDS}s with nothing changed; "
        "with the record fixed the next run changed nothing and kept the generation",
    )


@obligation("lifecycle.repair_without_disruption")
def test_repair_without_disruption(trial: TrialContext) -> CheckResult:
    """Deleted managed resources come back while production keeps serving."""
    cfg, cloud = trial.config, trial.cloud
    color = live_color(trial)
    standby = other(color)
    live_version = trial.manifest["release"]["live_version"]
    tasks = cloud.running_tasks(trial.cluster, trial.service_arn(color))

    # The fault: the preview listener and the standby service are deleted.
    cloud.elbv2.delete_listener(ListenerArn=trial.manifest["edge"]["preview_listener_arn"])
    try:
        cloud.ecs.update_service(cluster=trial.cluster, service=trial.service_arn(standby), desiredCount=0)
    except Exception:  # noqa: BLE001 - already scaled in is fine
        pass
    cloud.ecs.delete_service(cluster=trial.cluster, service=trial.service_arn(standby), force=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and cloud.service(trial.cluster, trial.service_arn(standby)) is not None:
        time.sleep(2)

    report, failure = release_under_traffic(trial, None, "deploy-repair")
    if failure:
        raise failure
    problems = traffic_problems(report, {live_version})
    routing = trial.routing()
    if routing["production"] != color or routing["preview"] != standby:
        problems.append(f"after repair production forwards to {routing['production']} and preview to {routing['preview']}")
    if cloud.service(trial.cluster, trial.service_arn(standby)) is None:
        problems.append(f"the {standby} service was not recreated (or the manifest does not name the new one)")
    if cloud.running_tasks(trial.cluster, trial.service_arn(color)) != tasks:
        problems.append(f"repair replaced live {color} tasks")
    release = trial.manifest["release"]
    if release["live_version"] != live_version or release["last_request"]["outcome"] != "unchanged":
        problems.append(f"the manifest release block is {release}")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "lifecycle.repair_without_disruption", Outcome.PASS,
        f"the preview listener and the {standby} service were restored without a failed production request",
        details={"requests": report.total},
    )


@obligation("lifecycle.reapply_stable")
def test_reapply_is_stable(trial: TrialContext) -> CheckResult:
    """A plan run directly against infra/ shows nothing to create or delete."""
    plan_json = plan(trial.config.infra_dir, artifact=trial.config.logs_dir / "standalone-plan.json")
    changes = disruptive_changes(plan_json)
    if changes:
        raise SubmissionFailure(
            f"a standalone plan wants to create or delete {len(changes)} resource(s): "
            + describe_changes(changes[:8]))
    return CheckResult(
        "lifecycle.reapply_stable", Outcome.PASS,
        "a standalone plan resolved every variable and shows nothing to create or delete",
        details={"resource_changes": len(plan_json.get("resource_changes", []))},
    )


@obligation("lifecycle.destroy_clean")
def test_destroy_is_clean(trial: TrialContext) -> CheckResult:
    """Destroy removes what this deployment owns and nothing else."""
    config = trial.config
    destroy(config.submission_dir, config.logs_dir)

    damaged = trial.cloud.decoys_intact()
    if damaged:
        raise CleanupLeak(f"destroy modified pre-existing resources: {damaged}")

    remaining = trial.cloud.prefixed_inventory(config.prefix)
    leaked = {kind: sorted(set(items) - set(trial.baseline.get(kind, []))) for kind, items in remaining.items()}
    leaked = {kind: items for kind, items in leaked.items() if items}
    if leaked:
        raise CleanupLeak(f"resources carrying this deployment's prefix remain: {leaked}")

    destroyed = {kind: sorted(set(items) - set(remaining.get(kind, []))) for kind, items in trial.baseline.items()}
    destroyed = {k: v for k, v in destroyed.items() if v}
    if destroyed:
        raise CleanupLeak(f"destroy removed resources it did not own: {destroyed}")

    return CheckResult(
        "lifecycle.destroy_clean", Outcome.PASS,
        "no resource carrying this deployment's prefix remains and the legacy resources are intact",
        details={"baseline_kinds": sorted(trial.baseline)},
    )
