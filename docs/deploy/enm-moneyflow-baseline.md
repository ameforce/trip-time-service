# ENM Moneyflow Baseline (TripTime Migration Reference)

## Purpose

This document captures the deployment baseline we must mirror from `moneyflow` for TripTime Jenkins + enm-server rollout.

## Investigation Scope

- Jenkins pipeline/job structure
- Branch routing (`main` vs non-main)
- Docker image/tag conventions
- enm-server runtime topology (container/proxy/network/rollback)

## Evidence Collection Result

Collection was attempted with both specialist subagents:

- `enm-jenkins-ops-specialist`
- `enm-server-ops-specialist`

Observed limits in the current execution environment:

- `jenkins.enmsoftware.com` access timed out.
- `enmsoftware.com:22` SSH access timed out.

Because of these network constraints, this baseline is recorded as **provisional** and must be finalized in an ENM-reachable environment.

## Confirmed TripTime Constraints (Repository Policy)

- Deployment path: Jenkins pipeline only.
- Domain routing:
  - `main` -> `https://triptime.enmsoftware.com`
  - non-main -> `https://dev.triptime.enmsoftware.com`
- Version format:
  - base tag: `vMAJOR.MINOR.PATCH`
  - runtime display: `vMAJOR.MINOR.PATCH.COMMIT`

## Moneyflow Baseline Fields To Finalize

Fill the following from real Jenkins/ENM access before production cutover:

1. Jenkins
   - Job type (multibranch/folder/pipeline)
   - Actual branch filters and trigger settings
   - Credential IDs (ID names only)
   - Stage ordering and rollback hooks
2. Image policy
   - Registry/repository naming
   - Tag composition rule (`version/branch/sha/build`)
3. enm-server
   - Container naming pattern (prod/dev)
   - Reverse proxy pattern (nginx or traefik)
   - Network and volume conventions
   - Rollback artifact location

## Required Follow-up Command Set (ENM Reachable Session)

Use this exact sequence in a reachable environment to finalize this document:

1. Jenkins UI/API read-only survey for moneyflow jobs and last successful builds (main/non-main).
2. enm-server SSH read-only survey:
   - `docker ps`
   - `docker inspect` for moneyflow containers
   - reverse proxy config inspection (`nginx` or `traefik`)
3. Capture sanitized outputs and update this file.

## Status

- Baseline investigation executed: yes
- Baseline fully verified against live moneyflow: no (network-restricted session)
