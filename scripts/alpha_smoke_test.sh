#!/usr/bin/env bash
set -euo pipefail

LABGPU_BIN="${LABGPU_BIN:-labgpu}"
export LABGPU_HOME="${LABGPU_HOME:-/tmp/labgpu-alpha-smoke}"

labgpu_cmd() {
  # Intentionally allow LABGPU_BIN="python3 -m labgpu" for source-tree checks.
  # shellcheck disable=SC2086
  $LABGPU_BIN "$@"
}

labgpu_cmd doctor
labgpu_cmd status --fake
labgpu_cmd status --fake --json >/dev/null

labgpu_cmd run --name smoke_success --gpu 0 -- bash -lc 'echo start; sleep 1; echo done'
sleep 2
labgpu_cmd refresh
labgpu_cmd list --all
labgpu_cmd logs smoke_success --tail 20
labgpu_cmd context smoke_success --tail 20

labgpu_cmd run --name smoke_fail --gpu 0 -- bash -lc 'echo "CUDA out of memory"; exit 1'
sleep 2
labgpu_cmd refresh
labgpu_cmd diagnose smoke_fail
labgpu_cmd context smoke_fail --tail 20

labgpu_cmd run --name smoke_kill --gpu 0 -- bash -lc 'sleep 999'
sleep 1
labgpu_cmd kill smoke_kill
sleep 1
labgpu_cmd refresh
labgpu_cmd list --all
