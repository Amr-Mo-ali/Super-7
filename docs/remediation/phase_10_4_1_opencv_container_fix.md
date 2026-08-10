# Phase 10.4.1 - OpenCV container compatibility fix

## Root cause

The application already declares `opencv-python-headless`, and no application code uses
OpenCV HighGUI/window APIs (`imshow`, `waitKey`, `namedWindow`, or equivalent).  However,
`ultralytics` also resolves the GUI-enabled `opencv-python` distribution.  Both distributions
provide the same `cv2` module.  The GUI-enabled wheel can therefore become the installed module
and attempts to load X11/XCB shared libraries during `import cv2`, producing:

```
ImportError: libxcb.so.1: cannot open shared object file
```

## Dependency and package changes

No application, API, model, or business-logic code changed.

The Docker image installation now:

1. installs the project dependencies;
2. removes the transitive GUI-enabled `opencv-python` package; and
3. force-reinstalls the declared `opencv-python-headless` package so its `cv2` files are the
   final installed files.

No Linux GUI/X11 packages were added.  They are unnecessary with the headless OpenCV wheel, so
this is smaller and avoids treating `libxcb1` as a permanent dependency for code that does not
use a display.

## Image size impact

The change removes the GUI-enabled OpenCV wheel from the final environment and adds no APT
packages.  The exact resulting image size could not be measured because Docker Desktop's Linux
engine was unavailable on the validation host.

## Validation result

The requested commands were attempted:

```powershell
docker compose build --no-cache
docker compose up
```

Neither could reach Docker Desktop:

```
failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine
open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified
```

Consequently, container-level verification remains pending:

- `cv2` import;
- model loading;
- FastAPI and Uvicorn startup; and
- Compose health endpoint availability.

## Remaining blocker

Start Docker Desktop (Linux containers engine), then rerun the two commands above.  Once the
container starts, verify `http://localhost:8000/openapi.json`; it is the configured Docker and
Compose health check target.
