# ADR-014 — Opt-in R2 video input for durable workers

Status: local implementation; no production role activation.

Durable worker roles default to `SUPER7_VIDEO_SOURCE=local`. Setting it to `r2`
selects Cloudflare R2 for claimed jobs only. The public `POST /analyze` shape stays
at `videoId`, `playerId`, `videoUrl`, and `callbackUrl`; `videoUrl` remains a plain
video filename. The API validates it without reaching either storage source.

The worker requires `SUPER7_R2_ENDPOINT` as an account-scoped HTTPS R2 S3 API
endpoint and dedicated `SUPER7_R2_ACCESS_KEY_ID` and
`SUPER7_R2_SECRET_ACCESS_KEY` values. Provision that credential with read-only
access scoped to the `4goats-media-dev` bucket. The worker signs S3 requests with
region `auto` and requests only `videos/<validated filename>`. There is no public
media-domain fallback. Put credentials in the worker's secret environment, not
the API or dispatcher environment, Git, or image layers.

The claimed worker creates a unique directory beneath `SUPER7_R2_TEMP_ROOT`
(system temp by default), downloads at most `MAX_UPLOAD_BYTES` (100 MiB by
default), checks free space before transfer, and removes the directory after the
child settles. The transfer uses `DOWNLOAD_TIMEOUT_SECONDS` (30 seconds by
default), single-attempt SDK calls, and five-second connect/read socket limits.
If a lease is lost during staging, the coordinator cancels staging before child
submission, stops that slot incarnation, and observes the outstanding task.
Cancellation waits for blocked SDK I/O or an already submitted child before
removing its file. A blocked native call can outlive the configured transfer
deadline by its socket timeout, and an uninterruptible OS-level call can delay
cleanup and worker shutdown. A hard process crash can leave a temporary file;
the worker's private temp root needs external stale-file housekeeping before
production activation. No process-count change is made here.

The existing four-field request and durable job row contain no trusted expected
SHA-256. The worker therefore cannot verify downloaded content against an
expected digest. S3/R2 ETag is not treated as SHA-256. Adding a trusted digest
requires a separate request and persistence contract decision, plus producer
validation. The current change confirms only declared length and bounded
transfer, not content integrity.
