# Phase 11.3 - Shared video volume integration

## Architecture

The backend writes uploaded video files to the host directory `/data/videos`. Docker mounts that
directory into Super-7 at `/videos` in read-only mode. The backend sends only the relative filename
in `videoUrl`; Super-7 resolves it under `VIDEO_STORAGE_ROOT=/videos` and never receives a backend
filesystem path.

```text
backend -> /data/videos -> Docker bind mount (/videos, read-only) -> Super-7 resolver -> analysis
```

## Mounting strategy

`docker-compose.yml` contains the bind mount:

```yaml
- /data/videos:/videos:ro
```

The container user can read files but cannot create, overwrite, or delete the backend's videos.
The resolver additionally validates file containment after resolving symlinks.

## Deployment procedure

1. Create `/data/videos` on the Docker host and grant the container runtime read and traversal
   access.
2. Configure the backend to store videos in `/data/videos`.
3. Set `VIDEO_STORAGE_ROOT=/videos` in Super-7's environment.
4. Start or redeploy with `docker compose up --build`.

At application startup, the FastAPI lifespan validates that the configured root exists, is a
directory, and is readable/traversable. Startup fails before serving requests when those checks
fail.

## Verification

1. Confirm the Docker mount with `docker compose exec football-analysis ls -la /videos`.
2. Call `GET /health/ready`; all `video_storage_*` checks must be true, including
   `video_storage_read_only`.
3. Submit a request with a known relative filename such as `video_001.mp4`.

## Rollback

1. Stop the service: `docker compose down`.
2. Remove the `/data/videos:/videos:ro` mount and restore the previous image/configuration.
3. Redeploy the prior service version.

No video data is changed by this phase because the mount is read-only.
