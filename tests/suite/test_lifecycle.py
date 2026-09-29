"""Lifecycle plane.

Releases, rollback, rejection, repair, plan stability and destruction, each
run under continuous production traffic. Faults are limited to what the
public contract describes: requesting a release, deleting managed resources
and destroying. Every check reads routing, tasks and responses live.
"""
from __future__ import annotations

import time

from .test_live import live_color, other, sample
from .tools.deployment import deploy, destroy
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

    lost = [q for q in trial.facts.get("quote_ids", []) + trial.facts.get("early_quotes", [])
            if trial.production.get(f"/quotes/{q}").status != 200]
    if lost:
        problems.append(f"{len(lost)} quotes written before or during the release no longer read back")

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
    good = cfg.good_release
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
