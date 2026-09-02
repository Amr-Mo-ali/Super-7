# CI/deployment separation discovery

Date: 2026-09-02

Inspected HEAD: `71d376361fe8e85b94e253726ce4d652e996339b`
Branch: `the-new-inhancement` (clean, two commits ahead of its origin)

This is a repository-only discovery. No workflow, runtime, test, Docker,
dependency, infrastructure, GitHub, or production change was made.

## Current workflow map

| File | Trigger | Purpose |
| --- | --- | --- |
| `.github/workflows/ci.yml` | `push` and `pull_request`, with no branch or path filters | Ruff check, format check, mypy, pytest, and Docker image build |
| `.github/workflows/deploy.yml` | `workflow_run` for completed `CI` runs on `main` | SSH deployment to the production VPS |

The CI workflow has no `workflow_run`, deployment job, environment reference,
or SSH command. The deployment workflow has no `push`, `pull_request`, or
`workflow_dispatch` trigger. Its job references the GitHub Environment
`production`, uses the `production-deploy` concurrency group with
`cancel-in-progress: false`, and invokes `appleboy/ssh-action@v1.2.0`.
Secrets are referenced but not printed or copied.

## Answers about implemented current behavior

1. **Deployment workflow:** `.github/workflows/deploy.yml`.
2. **Exact event:** `workflow_run` with `types: [completed]`, restricted to
   the `CI` workflow and branch `main`; the job runs only when
   `github.event.workflow_run.conclusion == 'success'`.
3. **Branches:** CI runs for pushes and pull requests on any branch because it
   has no filters. Deployment can run only for a completed successful CI run
   associated with `main`.
4. **Push behavior:** not every push deploys. A `main` push deploys only after
   its CI run completes successfully. Feature-branch pushes and pull requests
   run CI only.
5. **Revision deployed:** the remote script executes `git pull --ff-only
   origin main` and then builds Compose. It does not use
   `github.event.workflow_run.head_sha` or otherwise pin the reviewed SHA;
   therefore it deploys whatever fast-forward `main` resolves to at pull time.
6. **Overlap:** the workflow has a shared `production-deploy` concurrency group
   and `cancel-in-progress: false`, so queued deployments do not overlap.
7. **Approval gate:** the workflow references Environment `production`.
   Whether that environment has required reviewers, wait timers, or branch
   restrictions is a GitHub repository setting and cannot be verified locally.
8. **CI failure:** the deploy job condition is false, so deployment is not
   started.
9. **Deployment failure:** the SSH script uses `set -euo pipefail` and an ERR
   trap that prints Compose status and recent logs. Dirty-tree, wrong-branch,
   missing-repository, Compose validation/build, startup, and readiness
   failures return failure. Readiness is retried up to 20 attempts with a
   three-second delay.
10. **Healthy old container:** the clean-tree and pre-change validation guards
    leave the existing service untouched. After `docker compose up -d`, the
    repository does not implement an atomic replacement or explicit rollback;
    preservation of the old healthy container on build/start/health failure is
    therefore not guaranteed by the workflow. The operations handoff records
    the observed dirty-tree failure case preserving the old container.
11. **Rollback:** no automated rollback command or previous-SHA selection is
    present. Recovery is an operator action (restore/revert a reviewed `main`
    revision and rerun deployment).

Relevant deployment surfaces inspected were both workflows, `Dockerfile`,
`docker-compose.yml`, repository scripts, `docs/ci_cd_deployment.md`, and the
production operations/system handoffs. Compose defines one service with
`restart: unless-stopped`; neither Compose nor the workflows declares an
explicit stop grace period or deployment transaction mechanism.

Workflow history shows the deployment workflow was introduced in `f675329`,
then changed to successful-CI `workflow_run` behavior and error handling in
`a897866`; the current workflow also includes the temporary-path fix from
`1343b2c`.

## Coupling, risks, and classification

The current coupling is automatic: a successful CI completion on `main`
directly authorizes the deploy job. CI success is not a human approval, and
the deployment's unpinned `git pull` permits a later `main` commit to be
deployed if it arrives before the SSH step.

| Item | Classification |
| --- | --- |
| `push`/`pull_request` CI and successful-`workflow_run` deployment on `main` | Implemented current behavior |
| `production` Environment and its reviewer/branch rules | External GitHub setting; locally unknown |
| VPS secrets, branch protection, Actions permissions, and supervisor stop window | External/unknown; verify in GitHub or on the server without exposing values |
| Manual production dispatch, reviewed-SHA selection, and deployment separation | Proposed change |
| Transactional old-container preservation after `up -d` failure | Unknown/not guaranteed by current workflow |

## Smallest safe change plan

The preferred MVP is to leave `.github/workflows/ci.yml` unchanged and replace
the deployment workflow's automatic `workflow_run` trigger with
`workflow_dispatch`. Preserve the existing clean-working-tree guard, Compose
validation/build/start commands, readiness checks, and serialized production
concurrency group. Add no new platform or infrastructure.

However, trigger separation alone does not satisfy the reviewed-commit
requirement: the current SSH script still pulls the latest `origin main`.
Before implementation is approved, define a dispatch input for a reviewed
commit (or an equivalent reviewed ref), validate that it belongs to the
intended protected `main` history, and make the server build exactly that
revision. This is the smallest additional behavioral change needed to avoid
deploying a later unreviewed commit. It must be reviewed as part of the
implementation phase; it is not implemented here.

Do not add Kubernetes, GitOps, Terraform, release automation, cloud services,
or multi-environment orchestration. A GitHub Environment reviewer gate is a
useful optional hardening step, but it is not required to establish the MVP
manual trigger once reviewed-SHA handling is explicit.

## Safe rollout and rollback order

1. Inspect and record GitHub Environment protection, branch protection, Actions
   permissions, and the production supervisor's forced-stop window.
2. Prepare a separately reviewed workflow-only change: manual dispatch,
   reviewed-SHA validation/pinning, and retained production concurrency/guards.
3. Validate YAML and workflow static checks; run CI from a branch and pull
   request without any deployment trigger.
4. Merge the workflow change to protected `main` through the normal review and
   required checks. Do not dispatch production during this validation.
5. Manually dispatch the workflow for the exact reviewed SHA and verify Compose
   readiness and logs on the VPS, without recording secrets or addresses.
6. If the workflow change is unsafe, revert that workflow commit through the
   normal protected-branch process. Until then, the existing workflow remains
   the active deployment path.

Repository code alone cannot perform the required GitHub UI actions. Confirm
Environment reviewers/branch restrictions, branch protection required checks,
workflow permissions, who may dispatch production, and any Actions approval
policy before rollout.

## Verification plan and unresolved questions

For the implementation phase, parse both workflow YAML files with an existing
repository-available parser, run any existing workflow checks, run CI tests and
static checks, and exercise manual dispatch only with a disposable reviewed
revision. Verify that a failed CI run cannot dispatch deployment, that an
unreviewed or superseded SHA is rejected, that concurrent dispatches serialize,
and that readiness failure leaves a known healthy service or produces a tested
operator rollback path.

Unresolved or externally verified questions:

- What exact reviewer and branch rules are configured on Environment
  `production`?
- What branch protection and required-check rules apply to `main`?
- Can the VPS supervisor allow more than the approved five-second active
  analysis shutdown grace before forceful termination?
- Does the production host have a tested, recoverable previous-image/container
  rollback procedure?
- What exact reviewed-SHA input and server-side validation will be accepted?

**Recommendation:** NO-GO for changing only the trigger, because that would
still deploy an unpinned latest `main` state. GO for a separately reviewed
implementation phase that includes manual dispatch plus explicit reviewed-SHA
selection/validation, retaining the existing guards and deployment commands.
