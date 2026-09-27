"""Realized plane.

Live cloud APIs and live responses, not state. Proves the first deployment
really has one live color serving the initial release and an idle standby.
"""
from __future__ import annotations

import time

from .tools.api import Api
from .tools.errors import SubmissionFailure
from .tools.results import CheckResult, Outcome
from .tools.trial import TrialContext, obligation


def other(color: str) -> str:
    return "green" if color == "blue" else "blue"


def sample(api: Api, count: int) -> list:
    return [api.get("/release") for _ in range(count)]


def live_color(trial: TrialContext) -> str:
    routing = trial.routing()
    color = routing["production"]
    if color is None:
        raise SubmissionFailure(f"production does not forward to exactly one color: {routing['production_targets']}")
    if routing["preview"] != other(color):
        raise SubmissionFailure(f"preview forwards to {routing['preview_targets']}, expected the {other(color)} color")
    return color


@obligation("realized.initial_release")
def test_initial_release_is_live(trial: TrialContext) -> CheckResult:
    """One live color with the initial release, an idle standby."""
    cfg, cloud = trial.config, trial.cloud
    problems: list[str] = []
    color = live_color(trial)
    trial.facts["initial_live_color"] = color

    healthy = 0
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        healthy = cloud.healthy_targets(trial.target_group(color))
        if healthy >= cfg.desired:
            break
        time.sleep(5)
    if healthy < cfg.desired:
        problems.append(f"the live {color} target group has {healthy} healthy targets, expected {cfg.desired}")

    live_tasks = cloud.wait_running(trial.cluster, trial.service_arn(color), cfg.desired)
    if len(live_tasks) != cfg.desired:
        problems.append(f"the live {color} service runs {len(live_tasks)} tasks, expected {cfg.desired}")
    standby = cloud.wait_running(trial.cluster, trial.service_arn(other(color)), 0)
    if standby:
        problems.append(f"the standby {other(color)} service runs {len(standby)} tasks after the first deploy")
    for c in ("blue", "green"):
        service = cloud.service(trial.cluster, trial.service_arn(c))
        if service is None:
            problems.append(f"the {c} service does not exist")
            continue
        awsvpc = service.get("networkConfiguration", {}).get("awsvpcConfiguration", {})
        if awsvpc.get("assignPublicIp") == "ENABLED":
            problems.append(f"the {c} service assigns public IPs")

    responses = sample(trial.production, cfg.desired * 6)
    wrong = [(r.status, r.version, r.color) for r in responses
             if r.status != 200 or r.version != cfg.initial_release or r.color != color]
    if wrong:
        problems.append(f"production answered {len(wrong)} of {len(responses)} requests other than "
                        f"{cfg.initial_release}/{color}: {wrong[:4]}")
    tasks = {r.task for r in responses if r.task}
    if cfg.desired > 1 and len(tasks) < 2:
        problems.append(f"production was served by {len(tasks)} task(s)")

    release = trial.manifest["release"]
    if (release["live_version"], release["live_color"], release["standby_version"]) != (cfg.initial_release, color, None) \
            or release["last_request"]["outcome"] != "initial":
        problems.append(f"the manifest release block is {release}")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    trial.facts["initial_live_tasks"] = sorted(live_tasks)
    return CheckResult(
        "realized.initial_release", Outcome.PASS,
        f"{cfg.initial_release} is live in {color} on {cfg.desired} healthy tasks; {other(color)} is idle",
        details={"live_color": color, "tasks_seen": len(tasks)},
    )
