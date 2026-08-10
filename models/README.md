# Container model volume

This directory is intentionally excluded from the Docker build context and must not contain a model committed to Git.

For the default Compose configuration, place the required model at:

```text
models/yolo11n.pt
```

`docker-compose.yml` mounts this directory read-only at `/models`. `.env` configures both `MODEL_PATH` and `BALL_MODEL_PATH` as `/models/yolo11n.pt` by default. To use distinct models, place both files in this directory and set their two paths in `.env`.
