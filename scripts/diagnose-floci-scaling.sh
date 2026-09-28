#!/usr/bin/env bash
# Ground truth for BeaconFare on the exact Floci image the task uses:
# how does an ECS service behave when scaled up from desiredCount 0?
# Runs in ~4 minutes against your local Docker and cleans up after itself.
set -uo pipefail

NET=bfdiag
FLOCI_IMAGE='mirror.gcr.io/floci/floci:1.5.33@sha256:d2ecc8035822b23b8587a56eab15edd825f41d3fb80d93e8e66680410beddc08'
CLI_IMAGE='amazon/aws-cli:2.17.0'
TASK_IMAGE='mirror.gcr.io/library/python:3.12-slim'

cleanup() {
  docker ps -aq --filter "name=bfdiag" | xargs -r docker rm -f >/dev/null 2>&1
  docker rm -f bfdiag-floci >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT
cleanup

docker network create "$NET" >/dev/null
docker pull -q "$TASK_IMAGE" >/dev/null
docker pull -q "$CLI_IMAGE" >/dev/null
docker run -d --name bfdiag-floci --network "$NET" --network-alias aws \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e FLOCI_DOCKER_DOCKER_HOST=unix:///var/run/docker.sock \
  -e FLOCI_DOCKER_RESOURCE_NAMESPACE=bfdiag \
  -e FLOCI_HOSTNAME=aws -e FLOCI_DEFAULT_REGION=us-east-1 \
  -e FLOCI_SERVICES_ECS_MOCK=false \
  -e FLOCI_SERVICES_DOCKER_NETWORK="$NET" \
  -e FLOCI_SERVICES_ECS_DOCKER_NETWORK="$NET" \
  "$FLOCI_IMAGE" >/dev/null

aws() {
  docker run --rm -i --network "$NET" -e AWS_ACCESS_KEY_ID=test -e AWS_SECRET_ACCESS_KEY=test \
    -e AWS_DEFAULT_REGION=us-east-1 -e AWS_PAGER= "$CLI_IMAGE" --endpoint-url http://aws:4566 "$@"
}

echo "waiting for floci..."
for _ in $(seq 60); do aws ecs list-clusters >/dev/null 2>&1 && break; sleep 2; done

VPC=$(aws ec2 create-vpc --cidr-block 10.9.0.0/16 --query Vpc.VpcId --output text)
SUBNET=$(aws ec2 create-subnet --vpc-id "$VPC" --cidr-block 10.9.1.0/24 --query Subnet.SubnetId --output text)
aws ecs create-cluster --cluster-name bfdiag >/dev/null
aws logs create-log-group --log-group-name /custom/bfdiag >/dev/null

td() {  # td <family> <marker>
  aws ecs register-task-definition --family "$1" --requires-compatibilities FARGATE --network-mode awsvpc \
    --cpu 256 --memory 512 --container-definitions "[{\"name\":\"app\",\"image\":\"$TASK_IMAGE\",\"essential\":true,
      \"command\":[\"python3\",\"-m\",\"http.server\",\"8080\"],\"environment\":[{\"name\":\"MARK\",\"value\":\"$2\"}],
      \"portMappings\":[{\"containerPort\":8080}],
      \"logConfiguration\":{\"logDriver\":\"awslogs\",\"options\":{\"awslogs-group\":\"/custom/bfdiag\",\"awslogs-region\":\"us-east-1\",\"awslogs-stream-prefix\":\"app\"}}}]" \
    --query taskDefinition.taskDefinitionArn --output text
}
TD1=$(td bfdiag-app v1); TD2=$(td bfdiag-app-two v2)
NETCFG="awsvpcConfiguration={subnets=[$SUBNET],assignPublicIp=DISABLED}"

svc() {  # svc <name> <td> <count>
  aws ecs create-service --cluster bfdiag --service-name "$1" --task-definition "$2" --desired-count "$3" \
    --launch-type FARGATE --network-configuration "$NETCFG" >/dev/null
}
running() { aws ecs describe-services --cluster bfdiag --services "$1" --query 'services[0].runningCount' --output text; }
wait_for() {  # wait_for <svc> <count> <seconds> -> prints final running count
  local end=$(( $(date +%s) + $3 )) n
  while :; do n=$(running "$1"); [ "$n" = "$2" ] && break; [ "$(date +%s)" -ge "$end" ] && break; sleep 5; done
  echo "$n"
}

echo "== control: created at 2"
svc s-control "$TD1" 2;             echo "control created@2 -> running $(wait_for s-control 2 90)"

echo "== A: created at 0, then desired 2 only"
svc s-a "$TD1" 0; sleep 10
aws ecs update-service --cluster bfdiag --service s-a --desired-count 2 >/dev/null
echo "A 0->2 same td -> running $(wait_for s-a 2 90)"

echo "== B: created at 0, then new td + desired 2 in ONE update"
svc s-b "$TD1" 0; sleep 10
aws ecs update-service --cluster bfdiag --service s-b --task-definition "$TD2" --desired-count 2 >/dev/null
B=$(wait_for s-b 2 90); echo "B 0->2 + td change -> running $B"
if [ "$B" != "2" ]; then
  aws ecs update-service --cluster bfdiag --service s-b --force-new-deployment >/dev/null
  echo "B after force-new-deployment -> running $(wait_for s-b 2 90)"
fi

echo "== C: created at 0, td change first, then desired 2 separately"
svc s-c "$TD1" 0; sleep 10
aws ecs update-service --cluster bfdiag --service s-c --task-definition "$TD2" >/dev/null; sleep 10
aws ecs update-service --cluster bfdiag --service s-c --desired-count 2 >/dev/null
echo "C td then 0->2 -> running $(wait_for s-c 2 90)"

echo "== E: running at 2, scale to 0, then back to 2 with a new td"
aws ecs update-service --cluster bfdiag --service s-control --desired-count 0 >/dev/null
echo "E 2->0 -> running $(wait_for s-control 0 60)"
aws ecs update-service --cluster bfdiag --service s-control --task-definition "$TD2" --desired-count 2 >/dev/null
echo "E 0->2 + td change -> running $(wait_for s-control 2 90)"

echo "== load-balanced variants (the real task attaches every service to a target group)"
SUBNET2=$(aws ec2 create-subnet --vpc-id "$VPC" --cidr-block 10.9.2.0/24 --availability-zone us-east-1b --query Subnet.SubnetId --output text)
LB=$(aws elbv2 create-load-balancer --name bfdiag-lb --subnets "$SUBNET" "$SUBNET2" --query 'LoadBalancers[0].LoadBalancerArn' --output text)
tg() { aws elbv2 create-target-group --name "$1" --protocol HTTP --port 8080 --target-type ip --vpc-id "$VPC" \
         --health-check-path / --query 'TargetGroups[0].TargetGroupArn' --output text; }
TGF=$(tg bfdiag-f); TGG=$(tg bfdiag-g); TGH=$(tg bfdiag-h); TGI=$(tg bfdiag-i)
aws elbv2 create-listener --load-balancer-arn "$LB" --protocol HTTP --port 80 \
  --default-actions "Type=forward,TargetGroupArn=$TGF" >/dev/null
aws elbv2 create-listener --load-balancer-arn "$LB" --protocol HTTP --port 8081 \
  --default-actions "Type=forward,TargetGroupArn=$TGG" >/dev/null
lbsvc() {  # lbsvc <name> <td> <count> <tg>
  aws ecs create-service --cluster bfdiag --service-name "$1" --task-definition "$2" --desired-count "$3" \
    --launch-type FARGATE --network-configuration "$NETCFG" \
    --load-balancers "targetGroupArn=$4,containerName=app,containerPort=8080" >/dev/null
}
lbparam() { echo "targetGroupArn=$1,containerName=app,containerPort=8080"; }

lbsvc s-f "$TD1" 0 "$TGF"; sleep 10
aws ecs update-service --cluster bfdiag --service s-f --desired-count 2 >/dev/null
echo "F [LB] created@0, desired 2 only -> running $(wait_for s-f 2 90)"

lbsvc s-g "$TD1" 0 "$TGG"; sleep 10
aws ecs update-service --cluster bfdiag --service s-g --task-definition "$TD2" --desired-count 2 >/dev/null
echo "G [LB] created@0, td + desired 2 -> running $(wait_for s-g 2 90)"

# Terraform re-sends loadBalancers and networkConfiguration on every UpdateService.
lbsvc s-h "$TD1" 0 "$TGH"; sleep 10
aws ecs update-service --cluster bfdiag --service s-h --task-definition "$TD2" --desired-count 2 \
  --load-balancers "$(lbparam "$TGH")" --network-configuration "$NETCFG" >/dev/null
H=$(wait_for s-h 2 90); echo "H [LB] created@0, td + desired 2 + LB/network re-sent (terraform style) -> running $H"
if [ "$H" != "2" ]; then
  aws ecs update-service --cluster bfdiag --service s-h --force-new-deployment >/dev/null
  echo "H after force-new-deployment -> running $(wait_for s-h 2 90)"
fi

lbsvc s-i "$TD1" 2 "$TGI"
echo "I [LB] created@2 -> running $(wait_for s-i 2 90)"
aws ecs update-service --cluster bfdiag --service s-i --task-definition "$TD2" \
  --load-balancers "$(lbparam "$TGI")" --network-configuration "$NETCFG" >/dev/null; sleep 20
echo "I [LB] running, td change (terraform style) -> running $(wait_for s-i 2 90)"
aws ecs update-service --cluster bfdiag --service s-i --desired-count 0 >/dev/null
echo "I [LB] 2->0 -> running $(wait_for s-i 0 60)"
aws ecs update-service --cluster bfdiag --service s-i --task-definition "$TD1" --desired-count 2 \
  --load-balancers "$(lbparam "$TGI")" --network-configuration "$NETCFG" >/dev/null
echo "I [LB] 0->2 + td (terraform style) -> running $(wait_for s-i 2 90)"

for s in s-f s-g s-h s-i; do
  echo "-- $s:"; aws ecs describe-services --cluster bfdiag --services "$s" \
    --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount,td:taskDefinition,deployments:deployments[].{status:status,td:taskDefinition,desired:desiredCount,running:runningCount,rollout:rolloutState},events:events[:3][].message}' --output json
done

echo "== logs: groups that exist after tasks ran (task def asked for /custom/bfdiag)"
aws logs describe-log-groups --query 'logGroups[].logGroupName' --output text

echo "== floci log lines mentioning ECS scaling or errors"
docker logs bfdiag-floci 2>&1 | grep -iE "ecs|reconcil|desired|error|exception" | grep -viE "DescribeServices|ListTasks|DescribeTasks" | tail -60
