"""Declaration plane.

These read Terraform or OpenTofu state. They prove the submission *declared*
the blue/green topology, the quotes table, the identities and the logging.
Where the endpoint records a setting without enforcing it (IAM, security
groups), that is all these checks claim.
"""
from __future__ import annotations

import fnmatch
import json
from typing import Any

from .tools.errors import SubmissionFailure
from .tools.results import CheckResult, Outcome
from .tools.terraform import by_type, state
from .tools.trial import TrialContext, obligation

REQUIRED_TYPES = {
    "aws_vpc": 1,
    "aws_subnet": 4,
    "aws_lb": 1,
    "aws_lb_listener": 2,
    "aws_lb_target_group": 2,
    "aws_ecs_cluster": 1,
    "aws_ecs_service": 2,
    "aws_ecs_task_definition": 2,
    "aws_iam_role": 2,
    "aws_dynamodb_table": 1,
    "aws_cloudwatch_log_group": 1,
}

TASK_ACTIONS = {"dynamodb:describetable", "dynamodb:getitem", "dynamodb:putitem", "dynamodb:deleteitem"}
LOG_ACTIONS = {"logs:createlogstream", "logs:putlogevents", "logs:createloggroup", "logs:describelogstreams"}


def _state(trial: TrialContext) -> dict[str, Any]:
    if "state" not in trial.facts:
        trial.facts["state"] = state(trial.config.infra_dir)
    return trial.facts["state"]


def _one(values: Any) -> dict[str, Any]:
    if isinstance(values, list):
        return values[0] if values else {}
    return values or {}


def _values(state_json: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [r["values"] for r in by_type(state_json, resource_type)]


def _listener_targets(listener: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    for action in listener.get("default_action") or []:
        if action.get("type") != "forward":
            continue
        if action.get("target_group_arn"):
            targets.append(action["target_group_arn"])
        for forward in action.get("forward") or []:
            for group in forward.get("target_group") or []:
                if group.get("arn") and group["arn"] not in targets:
                    targets.append(group["arn"])
    return targets


def _task_definition(state_json: dict[str, Any], reference: str) -> dict[str, Any] | None:
    for td in _values(state_json, "aws_ecs_task_definition"):
        if reference in (td.get("arn"), td.get("arn_without_revision"),
                         f"{td.get('family')}:{td.get('revision')}", td.get("family")):
            return td
    return None


def _containers(td: dict[str, Any]) -> list[dict[str, Any]]:
    raw = td.get("container_definitions") or "[]"
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _statements(policy: Any) -> list[dict[str, Any]]:
    if isinstance(policy, str):
        try:
            policy = json.loads(policy)
        except ValueError:
            return []
    statements = (policy or {}).get("Statement", [])
    return [statements] if isinstance(statements, dict) else list(statements)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else [str(v) for v in value]


def _role_policies(state_json: dict[str, Any], role: dict[str, Any]) -> list[dict[str, Any]]:
    """Every allow statement attached to a role, inline or managed."""
    name, arn = role.get("name"), role.get("arn")
    statements: list[dict[str, Any]] = []
    for inline in role.get("inline_policy") or []:
        statements.extend(_statements(inline.get("policy")))
    for policy in _values(state_json, "aws_iam_role_policy"):
        if policy.get("role") in (name, arn, role.get("id")):
            statements.extend(_statements(policy.get("policy")))
    managed = {p.get("arn"): p for p in _values(state_json, "aws_iam_policy")}
    for attachment in _values(state_json, "aws_iam_role_policy_attachment"):
        if attachment.get("role") in (name, arn) and attachment.get("policy_arn") in managed:
            statements.extend(_statements(managed[attachment["policy_arn"]].get("policy")))
    return [s for s in statements if s.get("Effect", "Allow") == "Allow"]


@obligation("declared.managed_iac")
def test_infrastructure_is_managed(trial: TrialContext) -> CheckResult:
    """Every scored resource family is declared in Terraform or OpenTofu."""
    state_json = _state(trial)
    missing = []
    for resource_type, minimum in REQUIRED_TYPES.items():
        found = len(by_type(state_json, resource_type))
        if found < minimum:
            missing.append(f"{resource_type}: expected at least {minimum}, found {found}")
    if missing:
        raise SubmissionFailure("required resources are not managed in state: " + "; ".join(missing))

    manifest = trial.manifest
    arns = {v.get("arn") for t in ("aws_lb", "aws_lb_listener", "aws_lb_target_group")
            for v in _values(state_json, t)}
    wanted = [manifest["edge"]["load_balancer_arn"], manifest["edge"]["production_listener_arn"],
              manifest["edge"]["preview_listener_arn"], *manifest["compute"]["target_groups"].values()]
    absent = [a for a in wanted if a not in arns]
    services = {v.get("id") for v in _values(state_json, "aws_ecs_service")} | \
               {v.get("arn") for v in _values(state_json, "aws_ecs_service")}
    absent += [a for a in manifest["compute"]["services"].values() if a not in services]
    tables = {v.get("name") for v in _values(state_json, "aws_dynamodb_table")}
    if trial.quotes_table not in tables:
        absent.append(trial.quotes_table)
    if absent:
        raise SubmissionFailure(f"resources named in the manifest are not managed in state: {absent}")
    return CheckResult(
        "declared.managed_iac", Outcome.PASS,
        "every scored resource family is managed and the manifest resolves to state",
        details={"types": {t: len(by_type(state_json, t)) for t in REQUIRED_TYPES}},
    )


@obligation("declared.release_topology")
def test_release_topology(trial: TrialContext) -> CheckResult:
    """Two listeners, two color target groups, two color services."""
    state_json = _state(trial)
    cfg = trial.config
    problems: list[str] = []
    tg_color = {trial.target_group(c): c for c in ("blue", "green")}

    listeners = {v.get("arn"): v for v in _values(state_json, "aws_lb_listener")}
    forwards: dict[str, str | None] = {}
    for name, port, arn in (("production", cfg.production_port, trial.manifest["edge"]["production_listener_arn"]),
                            ("preview", cfg.preview_port, trial.manifest["edge"]["preview_listener_arn"])):
        listener = listeners.get(arn)
        if listener is None:
            problems.append(f"the {name} listener is not in state")
            continue
        if int(listener.get("port") or 0) != port:
            problems.append(f"the {name} listener is on port {listener.get('port')}, expected {port}")
        if (listener.get("protocol") or "HTTP").upper() != "HTTP":
            problems.append(f"the {name} listener protocol is {listener.get('protocol')}")
        targets = _listener_targets(listener)
        if len(targets) != 1 or targets[0] not in tg_color:
            problems.append(f"the {name} listener must forward to exactly one color target group, found {targets}")
            forwards[name] = None
        else:
            forwards[name] = tg_color[targets[0]]
    if forwards.get("production") and forwards.get("production") == forwards.get("preview"):
        problems.append("production and preview forward to the same color")

    groups = {v.get("arn"): v for v in _values(state_json, "aws_lb_target_group")}
    for color in ("blue", "green"):
        tg = groups.get(trial.target_group(color))
        if tg is None:
            problems.append(f"the {color} target group is not in state")
            continue
        # awsvpc tasks register by IP; the port that matters is the container
        # port the service registers, checked below.
        if tg.get("target_type") != "ip":
            problems.append(f"the {color} target group target type is {tg.get('target_type')!r}, expected 'ip'")
        if _one(tg.get("health_check")).get("path") != "/health/ready":
            problems.append(f"the {color} target group health check path is {_one(tg.get('health_check')).get('path')}")

    private = set(trial.manifest["network"]["private_subnet_ids"])
    images = set(cfg.images.values())
    seen_colors = set()
    for service in _values(state_json, "aws_ecs_service"):
        attached = [lb for lb in service.get("load_balancer") or [] if lb.get("target_group_arn") in tg_color]
        if not attached:
            continue
        color = tg_color[attached[0]["target_group_arn"]]
        if len({tg_color[lb["target_group_arn"]] for lb in attached}) > 1:
            problems.append(f"service {service.get('name')} is attached to both colors")
        seen_colors.add(color)
        if any(int(lb.get("container_port") or 0) != 8080 for lb in attached):
            problems.append(f"the {color} service does not register container port 8080")
        network = _one(service.get("network_configuration"))
        subnets = set(network.get("subnets") or [])
        if not subnets or not subnets <= private:
            problems.append(f"the {color} service runs outside the private subnets: {sorted(subnets - private)}")
        if network.get("assign_public_ip"):
            problems.append(f"the {color} service assigns public IPs")
        td = _task_definition(state_json, service.get("task_definition") or "")
        if td is None:
            problems.append(f"the {color} service's task definition is not managed in state")
            continue
        containers = _containers(td)
        if not containers:
            problems.append(f"the {color} task definition has no readable container definitions")
            continue
        container = containers[0]
        if container.get("image") not in images:
            problems.append(f"the {color} task definition uses image {container.get('image')!r}")
        env = {e.get("name"): e.get("value") for e in container.get("environment") or []}
        if env.get("DEPLOYMENT_COLOR") != color:
            problems.append(f"the {color} task definition sets DEPLOYMENT_COLOR={env.get('DEPLOYMENT_COLOR')!r}")
    if seen_colors != {"blue", "green"}:
        problems.append(f"color services attached to their target groups: {sorted(seen_colors)}")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "declared.release_topology", Outcome.PASS,
        "two listeners forward to two different color target groups served by two private color services",
        details={"forwards": forwards},
    )


@obligation("declared.identity_data_logs")
def test_identity_data_logs(trial: TrialContext) -> CheckResult:
    """Quotes table, least-privilege roles, logging and retention."""
    state_json = _state(trial)
    problems: list[str] = []

    table = next((t for t in _values(state_json, "aws_dynamodb_table") if t.get("name") == trial.quotes_table), {})
    hash_key = table.get("hash_key")
    for entry in table.get("key_schema") or []:
        if (entry.get("key_type") or "").upper() == "HASH":
            hash_key = hash_key or entry.get("attribute_name")
    if hash_key != "quote_id" or table.get("range_key"):
        problems.append(f"the quotes table key is {hash_key}/{table.get('range_key')}, expected quote_id only")
    types = {a.get("name"): a.get("type") for a in table.get("attribute") or []}
    if types.get("quote_id") != "S":
        problems.append("quote_id is not declared as S")
    if table.get("billing_mode") != "PAY_PER_REQUEST":
        problems.append(f"the quotes table billing mode is {table.get('billing_mode')!r}")
    ttl = _one(table.get("ttl"))
    if not ttl.get("enabled") or ttl.get("attribute_name") != "expires_at":
        problems.append(f"the quotes table TTL is {ttl or 'absent'}")

    roles = {r.get("arn"): r for r in _values(state_json, "aws_iam_role")}
    table_arn = trial.manifest["data"]["quotes_table"]["arn"]
    for kind, allowed in (("execution_role_arn", LOG_ACTIONS), ("task_role_arn", TASK_ACTIONS)):
        role = roles.get(trial.manifest["roles"][kind])
        if role is None:
            problems.append(f"{kind} is not a role in state")
            continue
        statements = _role_policies(state_json, role)
        if kind == "task_role_arn" and not statements:
            problems.append("the task role has no policy")
        for statement in statements:
            actions = [a.lower() for a in _as_list(statement.get("Action"))]
            resources = _as_list(statement.get("Resource"))
            if statement.get("NotAction") or statement.get("NotResource"):
                problems.append(f"{kind} uses NotAction/NotResource")
            if any("*" in a for a in actions):
                problems.append(f"{kind} grants a wildcard action {actions}")
            if "*" in resources:
                problems.append(f"{kind} grants Resource '*'")
            extra = sorted(set(actions) - allowed)
            if extra:
                problems.append(f"{kind} grants actions outside its purpose: {extra}")
            if kind == "task_role_arn":
                other = [r for r in resources if not fnmatch.fnmatchcase(r, table_arn)]
                if other:
                    problems.append(f"the task role reaches resources other than the quotes table: {other}")

    groups = {g.get("name"): g for g in _values(state_json, "aws_cloudwatch_log_group")}
    for name in trial.manifest["logs"]["groups"]:
        group = groups.get(name)
        if group is None:
            problems.append(f"log group {name} is not managed in state")
        elif int(group.get("retention_in_days") or 0) != trial.config.log_retention_days:
            problems.append(f"log group {name} retains {group.get('retention_in_days')} days, "
                            f"expected {trial.config.log_retention_days}")
    # The endpoint does not echo logConfiguration back into state; it writes
    # every task's output to /ecs/<family>. That group must be managed, or it
    # is created outside state and leaks past destroy.
    for td in _values(state_json, "aws_ecs_task_definition"):
        expected = f"/ecs/{td.get('family')}"
        group = groups.get(expected)
        if group is None:
            problems.append(f"task definition family {td.get('family')} has no managed log group {expected}")
        elif int(group.get("retention_in_days") or 0) != trial.config.log_retention_days:
            problems.append(f"log group {expected} retains {group.get('retention_in_days')} days, "
                            f"expected {trial.config.log_retention_days}")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "declared.identity_data_logs", Outcome.PASS,
        "quotes table, least-privilege roles and retained awslogs logging are declared as contracted",
    )
