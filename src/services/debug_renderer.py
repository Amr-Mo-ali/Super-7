"""Non-destructive visual diagnostics for a completed analysis."""

from pathlib import Path

import cv2

from services.ball_tracker import BallTrackPoint
from services.interactions.models import InteractionAnalysisResult
from services.player_detector import BoundingBox
from services.selection import Selection
from services.technical_events.models import TechnicalEventAnalysisResult


def render_debug_video(
    source: Path,
    output_dir: Path,
    selection: Selection,
    player_boxes: dict[int, dict[int, BoundingBox]] | None,
    ball_points: dict[int, BallTrackPoint] | None,
    interactions: InteractionAnalysisResult | None,
    events: TechnicalEventAnalysisResult | None,
) -> dict[str, str]:
    """Render overlays into new files; the uploaded source is opened read-only."""
    del interactions, events  # Their ranges are represented by candidate IDs in API diagnostics.
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "debug_frames"
    frames_dir.mkdir(exist_ok=True)
    capture = cv2.VideoCapture(str(source))
    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    width, height = (
        int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
        int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    )
    target = output_dir / "debug_video.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]
    writer = cv2.VideoWriter(
        str(target),
        fourcc,
        fps,
        (width, height),
    )
    frame = 0
    boxes = (player_boxes or {}).get(selection.track.track_id, {})
    trajectory: list[tuple[int, int]] = []
    while True:
        ok, image = capture.read()
        if not ok:
            break
        box = boxes.get(frame)
        if box:
            x1, y1, x2, y2 = int(box.x1), int(box.y1), int(box.x2), int(box.y2)
            cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(image, "selected player", (x1, max(18, y1 - 5)), 0, 0.5, (0, 255, 0), 1)
        point = (ball_points or {}).get(frame)
        if point and point.center_point:
            center = (int(point.center_point[0]), int(point.center_point[1]))
            trajectory.append(center)
            cv2.circle(image, center, 5, (0, 165, 255), -1)
        for left, right in zip(trajectory, trajectory[1:], strict=False):
            cv2.line(image, left, right, (0, 165, 255), 2)
        writer.write(image)
        cv2.imwrite(str(frames_dir / f"frame_{frame:06d}.jpg"), image)
        frame += 1
    writer.release()
    capture.release()
    return {"debug_video": str(target), "debug_frames": str(frames_dir)}
