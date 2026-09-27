"""Cloud API access for the verifier. Live answers, never Terraform state."""
from __future__ import annotations

import time
from typing import Any

import boto3

from .config import Config


class Cloud:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}

    def client(self, service: str):
        if service not in self._clients:
            self._clients[service] = boto3.client(
                service, region_name=self._config.region, endpoint_url=self._config.endpoint_url)
        return self._clients[service]

    @property
    def ddb(self):
        return self.client("dynamodb")

    @property
    def elbv2(self):
        return self.client("elbv2")

    @property
    def ecs(self):
        return self.client("ecs")

    @property
    def ec2(self):
        return self.client("ec2")

    @property
    def logs(self):
        return self.client("logs")

    # -- load balancer ----------------------------------------------------------
    def healthy_targets(self, target_group_arn: str) -> int:
        described = self.elbv2.describe_target_health(TargetGroupArn=target_group_arn)
        return sum(1 for entry in described["TargetHealthDescriptions"]
                   if entry["TargetHealth"]["State"] == "healthy")

    def listeners(self, load_balancer_arn: str) -> dict[int, dict[str, Any]]:
        found = self.elbv2.describe_listeners(LoadBalancerArn=load_balancer_arn)["Listeners"]
        return {int(listener["Port"]): listener for listener in found}

    @staticmethod
    def forward_targets(listener: dict[str, Any]) -> list[str]:
        """Target groups a listener's default action forwards to."""
        targets: list[str] = []
        for action in listener.get("DefaultActions", []):
            if action.get("Type") != "forward":
                continue
            if action.get("TargetGroupArn"):
                targets.append(action["TargetGroupArn"])
            for group in (action.get("ForwardConfig") or {}).get("TargetGroups", []):
                if group.get("TargetGroupArn") and group["TargetGroupArn"] not in targets:
                    targets.append(group["TargetGroupArn"])
        return targets

    # -- ECS --------------------------------------------------------------------
    def service(self, cluster: str, service_arn: str) -> dict[str, Any] | None:
        try:
            found = self.ecs.describe_services(cluster=cluster, services=[service_arn])["services"]
        except Exception:  # noqa: BLE001 - a missing cluster means a missing service
            return None
        if not found or found[0].get("status") not in ("ACTIVE", "DRAINING"):
            return None
        return found[0]

    def running_tasks(self, cluster: str, service_arn: str) -> set[str]:
        service = self.service(cluster, service_arn)
        if service is None:
            return set()
        arns: set[str] = set()
        kwargs: dict[str, Any] = {"cluster": cluster, "serviceName": service["serviceName"],
                                  "desiredStatus": "RUNNING"}
        while True:
            page = self.ecs.list_tasks(**kwargs)
            arns.update(page.get("taskArns", []))
            if not page.get("nextToken"):
                break
            kwargs["nextToken"] = page["nextToken"]
        if not arns:
            return arns
        described = self.ecs.describe_tasks(cluster=cluster, tasks=sorted(arns))["tasks"]
        return {t["taskArn"] for t in described if t.get("lastStatus") == "RUNNING"}

    def wait_running(self, cluster: str, service_arn: str, count: int, timeout: int = 60) -> set[str]:
        deadline = time.monotonic() + timeout
        tasks: set[str] = set()
        while time.monotonic() < deadline:
            tasks = self.running_tasks(cluster, service_arn)
            if len(tasks) == count:
                return tasks
            time.sleep(3)
        return tasks

    # -- DynamoDB -----------------------------------------------------------------
    def table(self, name: str) -> dict[str, Any] | None:
        try:
            return self.ddb.describe_table(TableName=name)["Table"]
        except self.ddb.exceptions.ResourceNotFoundException:
            return None

    # -- decoys and inventory -----------------------------------------------------
    def create_decoys(self) -> None:
        """Pre-existing resources that share the prefix but are not owned."""
        cfg = self._config
        self.ddb.create_table(
            TableName=cfg.legacy_table, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "quote_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "quote_id", "KeyType": "HASH"}],
            Tags=[{"Key": "Owner", "Value": "legacy-platform"}],
        )
        self.ddb.put_item(TableName=cfg.legacy_table,
                          Item={"quote_id": {"S": "LEGACY-1"}, "note": {"S": "do not delete"}})
        self.ecs.create_cluster(clusterName=cfg.legacy_cluster,
                                tags=[{"key": "Owner", "value": "legacy-platform"}])
        self.logs.create_log_group(logGroupName=cfg.legacy_log_group)

    def decoys_intact(self) -> list[str]:
        cfg = self._config
        problems = []
        if self.table(cfg.legacy_table) is None:
            problems.append(f"table {cfg.legacy_table} was deleted")
        elif not self.ddb.get_item(TableName=cfg.legacy_table,
                                   Key={"quote_id": {"S": "LEGACY-1"}}).get("Item"):
            problems.append(f"table {cfg.legacy_table} lost its data")
        clusters = self.ecs.describe_clusters(clusters=[cfg.legacy_cluster]).get("clusters", [])
        if not clusters or clusters[0].get("status") != "ACTIVE":
            problems.append(f"cluster {cfg.legacy_cluster} was deleted")
        groups = self.logs.describe_log_groups(logGroupNamePrefix=cfg.legacy_log_group)["logGroups"]
        if not any(g["logGroupName"] == cfg.legacy_log_group for g in groups):
            problems.append(f"log group {cfg.legacy_log_group} was deleted")
        return problems

    def prefixed_inventory(self, prefix: str) -> dict[str, list[str]]:
        """Live resources carrying this deployment's prefix or tag."""
        found: dict[str, list[str]] = {}

        def record(kind: str, names) -> None:
            hits = sorted(str(n) for n in names if prefix in str(n))
            if hits:
                found[kind] = hits

        def services() -> list[str]:
            arns: list[str] = []
            for cluster in self.ecs.list_clusters()["clusterArns"]:
                if prefix not in cluster:
                    continue
                for arn in self.ecs.list_services(cluster=cluster).get("serviceArns", []):
                    described = self.ecs.describe_services(cluster=cluster, services=[arn])["services"]
                    if described and described[0].get("status") == "ACTIVE":
                        arns.append(arn)
            return arns

        def active_clusters() -> list[str]:
            arns = self.ecs.list_clusters()["clusterArns"]
            if not arns:
                return []
            described = self.ecs.describe_clusters(clusters=arns)["clusters"]
            return [c["clusterArn"] for c in described if c.get("status") == "ACTIVE"]

        probes = {
            "tables": lambda: self.ddb.list_tables().get("TableNames", []),
            "load_balancers": lambda: [lb["LoadBalancerArn"] for lb in
                                       self.elbv2.describe_load_balancers()["LoadBalancers"]],
            "target_groups": lambda: [tg["TargetGroupArn"] for tg in
                                      self.elbv2.describe_target_groups()["TargetGroups"]],
            "clusters": active_clusters,
            "services": services,
            "log_groups": lambda: [g["logGroupName"] for g in
                                   self.logs.describe_log_groups()["logGroups"]],
        }
        for kind, probe in probes.items():
            try:
                record(kind, probe())
            except Exception:  # noqa: BLE001 - absent service means nothing to report
                pass
        try:
            vpcs = [vpc["VpcId"] for vpc in self.ec2.describe_vpcs()["Vpcs"]
                    for tag in vpc.get("Tags", [])
                    if tag.get("Key") == "BeaconFareDeployment" and tag.get("Value") == prefix]
            if vpcs:
                found["vpcs"] = sorted(vpcs)
        except Exception:  # noqa: BLE001
            pass
        return found
