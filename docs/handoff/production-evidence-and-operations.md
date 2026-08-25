# Production evidence and operations

## Evidence boundary

The following are supplied empirical observations, not repository-derived benchmarks. Exact observation dates and deployed commit hashes were not supplied and are therefore **unknown**. Environment: Hostinger VPS, Docker Compose, CPU analysis, 4 vCPU, approximately 15.6 GiB RAM, queue capacity observed as 10, and max active analyses observed as 1.

| Observation | Result |
|---|---|
| 1: approximately 54.93 s video, first request after deployment | analysis 294,985 ms; callback 29 ms; end-to-end 295,016 ms; queue wait 2 ms; callback delivered attempt 1; `COMPLETED`; child PID observed; includes child initialization |
| 2: different 14.17 s shooting drill | analysis 59,367 ms; callback 29 ms; end-to-end 59,397 ms; queue wait 1 ms; callback delivered attempt 1; `COMPLETED`; no second `analysis_child_initialized`; child reuse observed |
| Resource observation for shooting video | active CPU commonly 305–315%; peak 340.82%; memory peak about 706 MiB |

Inference: one analysis used roughly three to 3.4 CPU cores in the observed active phase. Memory was not the apparent constraint; CPU likely was. Two parallel analyses on this 4-vCPU host would likely contend substantially, which is evidence against casually changing to two processes. The videos differ, so this is not a cold-versus-warm speed comparison or validation dataset.

## Operations

Keep diagnostics outside the production checkout (for example `/opt/Super-7-diagnostics/`). Record timestamp, deployed commit, sanitized analysis ID, container state, lifecycle events, resources and callback outcome; never record callback URLs, tokens, bodies, server addresses or player data.

```bash
cd /opt/Super-7
git status --short
git rev-parse HEAD
docker compose ps
docker compose logs --timestamps football-analysis
docker compose logs --timestamps football-analysis | grep '<sanitized-analysis-id>'
docker compose logs --timestamps football-analysis | grep -iE 'error|failed|exhausted'
docker stats --no-stream
```

For callback outcome, correlate `analysis_callback_attempt_finished`, `analysis_callback_finished`, then `analysis_job_terminal`. For child reuse, retain the startup `analysis_child_initialized` event and confirm no subsequent initializer event while sequential requests complete. Before deployment, require clean `git status --short`; the deploy workflow also requires `main`, fast-forward pull, Compose validation and readiness ([workflow](../../.github/workflows/deploy.yml)).

Incident record: manually generated `super7-*.log` files inside `/opt/Super-7` made the clean-tree guard stop deployment. It correctly left the healthy old container in place. Moving logs to `/opt/Super-7-diagnostics/` allowed deployment. Future evidence must stay outside the checkout.

Observed logs included repeated `/openapi.json` requests because the Docker healthcheck uses that endpoint. The service is exposed on `0.0.0.0:8000`, and an unsolicited external request was observed. Direct exposure, firewall and reverse-proxy policy need a separate security review; no networking change is made here.
