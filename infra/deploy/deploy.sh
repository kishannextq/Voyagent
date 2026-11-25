#!/usr/bin/env bash
# Roll a new image tag out to one environment's ECS services, with migrations first and an
# automatic rollback to the previous task definitions if anything fails.
#
#   CLUSTER=voyagent-staging SUBNETS=subnet-a,subnet-b SECURITY_GROUP=sg-123 \
#   ECR_API=... ECR_WEB=... ECR_GATEWAY=... IMAGE_TAG=<git sha> HEALTH_URL=https://host/api/v1/health \
#   ./deploy.sh
#
# Requires: aws CLI v2, jq. Credentials come from the GitHub OIDC deploy role (ci_oidc module).
# Not yet executed against a real AWS account.
set -euo pipefail

: "${CLUSTER:?}" "${SUBNETS:?}" "${SECURITY_GROUP:?}" "${IMAGE_TAG:?}" "${HEALTH_URL:?}"
: "${ECR_API:?}" "${ECR_WEB:?}" "${ECR_GATEWAY:?}"

declare -A IMAGE=(
  [web]="$ECR_WEB:$IMAGE_TAG"
  [api]="$ECR_API:$IMAGE_TAG"
  [orchestrator]="$ECR_API:$IMAGE_TAG"
  [ingest]="$ECR_API:$IMAGE_TAG"
  [gateway]="$ECR_GATEWAY:$IMAGE_TAG"
)
ORDER=(gateway orchestrator ingest api web)   # backends first, public entry points last
declare -A PREVIOUS=()

log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

# New revision of a task definition family with only the image changed.
register() {
  local family=$1 image=$2
  aws ecs describe-task-definition --task-definition "$family" --query taskDefinition --output json \
    | jq --arg img "$image" '.containerDefinitions[0].image = $img
        | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
              .registeredAt, .registeredBy, .deregisteredAt)' > /tmp/td.json
  aws ecs register-task-definition --cli-input-json file:///tmp/td.json \
    --query taskDefinition.taskDefinitionArn --output text
}

rollback() {
  log "ROLLBACK: restoring previous task definitions"
  for svc in "${!PREVIOUS[@]}"; do
    aws ecs update-service --cluster "$CLUSTER" --service "$svc" --task-definition "${PREVIOUS[$svc]}" > /dev/null || true
  done
  aws ecs wait services-stable --cluster "$CLUSTER" --services "${!PREVIOUS[@]}" || true
  log "rollback finished"
}
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ "${#PREVIOUS[@]}" -gt 0 ]; then
    rollback
  fi
  exit "$rc"
}
trap on_exit EXIT

# 1. Migrations (expand-only, backward compatible) as a one-off task with the new image.
family_prefix=$CLUSTER
migrate_td=$(register "$family_prefix-migrate" "$ECR_API:$IMAGE_TAG")
log "running migrations ($migrate_td)"
task=$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE --task-definition "$migrate_td" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SECURITY_GROUP],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task"
code=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" --query 'tasks[0].containers[0].exitCode' --output text)
[ "$code" = "0" ] || { log "migrations failed (exit $code)"; exit 1; }
log "migrations ok"

# Keep the maintenance job on the same image.
register "$family_prefix-maintenance" "$ECR_API:$IMAGE_TAG" > /dev/null

# 2. Services, one at a time; the ECS circuit breaker also rolls back a service that can't stabilise.
for svc in "${ORDER[@]}"; do
  PREVIOUS[$svc]=$(aws ecs describe-services --cluster "$CLUSTER" --services "$svc" \
    --query 'services[0].taskDefinition' --output text)
  td=$(register "$family_prefix-$svc" "${IMAGE[$svc]}")
  log "deploying $svc -> $td"
  aws ecs update-service --cluster "$CLUSTER" --service "$svc" --task-definition "$td" > /dev/null
  aws ecs wait services-stable --cluster "$CLUSTER" --services "$svc"
done

# 3. Smoke test through the public entry point.
for _ in $(seq 1 20); do
  if curl -sf --max-time 5 "$HEALTH_URL" > /dev/null; then
    log "deploy of $IMAGE_TAG to $CLUSTER succeeded"
    PREVIOUS=()   # nothing to roll back
    exit 0
  fi
  sleep 6
done
log "smoke test failed: $HEALTH_URL"
exit 1
