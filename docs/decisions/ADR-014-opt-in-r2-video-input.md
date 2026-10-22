# ADR-014 — Opt-in R2 video input for durable workers

Status: local implementation; no production role activation.

Durable worker roles default to `SUPER7_VIDEO_SOURCE=local`. Setting it to `r2`
selects Cloudflare R2 for claimed jobs only. The legacy public `POST /analyze`
shape stays at `videoId`, `playerId`, `videoUrl`, and `callbackUrl`; `videoUrl`
remains a plain video filename. The verified extension is described below.
The API validates either shape without reaching either storage source.

The worker requires `SUPER7_R2_ENDPOINT` as an account-scoped HTTPS R2 S3 API
endpoint and dedicated `SUPER7_R2_ACCESS_KEY_ID` and
`SUPER7_R2_SECRET_ACCESS_KEY` values. Provision that credential with read-only
access scoped to the `4goats-media-dev` bucket. The worker signs S3 requests with
region `auto`, uses virtual bucket addressing, and requests only
`videos/<validated filename>`. There is no public
media-domain fallback. Put credentials in the worker's secret environment, not
the API or dispatcher environment, Git, or image layers.
The supplied non-secret endpoint for this integration is
`https://af887b39dab74d7dfee3ccda49cb4b28.r2.cloudflarestorage.com`;
the configured region is `auto`, fixed bucket `4goats-media-dev`, fixed key
prefix `videos/`, and path-style addressing is disabled. The read-only
bucket-scoped credential must arrive through a private secret channel, never
from the Apex backend's read/write credential.

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

The existing four-field request has no trusted expected SHA-256. S3/R2 ETag is
not treated as SHA-256. The optional verified contract accepts `videoKey`,
`videoSizeBytes`, `videoSha256`, and `hashAlgorithm` together. The key must be
`videos/<videoUrl>`, the algorithm `sha256`, size a positive integer, and digest
64 lowercase hex characters. Legacy fingerprint v1 remains unchanged;
verified requests use fingerprint v2 with the four added values. Slice 5 stores
the expected size and binary digest in the admission transaction. The worker
checks streamed bytes and SHA-256 before submitting the child. Caller-provided
metadata does not independently prove that the intended video was authorized.
The runtime PostgreSQL role needs `SELECT` on `analysis_jobs.video_sha256` for
admission replay, worker claim and recovery, and job finalization. The existing
table-level `SELECT` grant includes this new column when Slice 5 alters the
table; PostgreSQL cannot deny one column with a column-level `REVOKE` while
that table grant remains. The role has no `DELETE` privilege, and write access
is granted separately during runtime setup.
An integrity mismatch, incomplete transfer, or verified claim on a local-only
worker stops that worker incarnation without a terminal callback. The claim
remains `RUNNING` with an active attempt until its lease expires and a running
worker performs recovery. A replacement worker must have R2 configured to
complete the verified job; without a running recovery actor the row can remain
`RUNNING` indefinitely. This is an operational requirement before activation.
No server-side known-object read with the dedicated credential has been run
for this patch; R2 connectivity and authorization remain unverified.
