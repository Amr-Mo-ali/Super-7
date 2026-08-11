"""Ultralytics adapter that exposes only validated sports-ball detections."""

import logging
from collections.abc import Sequence
from time import perf_counter
from typing import Any

import numpy as np

from core.config import Settings
from core.exceptions import BallDetectionError, BallDetectorInitializationError, InvalidFrameError
from services.ball_detector import BallDetection, BallDetector
from services.player_detector import BoundingBox

_SPORTS_BALL_CLASS = 32


class YOLOBallDetector(BallDetector):
    def __init__(
        self, settings: Settings, logger: logging.Logger, model: Any | None = None
    ) -> None:
        self._settings = settings
        self._logger = logger
        self._model = model

    def _load_model(self) -> Any:
        try:
            from ultralytics import YOLO  # type: ignore[attr-defined]

            model = YOLO(self._settings.ball_model_path)
            self._logger.info(
                "ball_model_loaded path=%s device=%s",
                self._settings.ball_model_path,
                self._settings.model_device,
            )
            return model
        except Exception as error:
            raise BallDetectorInitializationError(
                f"Unable to load ball detector: {error}"
            ) from error

    def detect(
        self, frame: np.ndarray, frame_index: int, timestamp_seconds: float
    ) -> Sequence[BallDetection]:
        if (
            not isinstance(frame, np.ndarray)
            or frame.size == 0
            or frame.ndim != 3
            or frame.shape[2] != 3
        ):
            raise InvalidFrameError("Ball detector frame must be a non-empty three-channel image.")
        started = perf_counter()
        try:
            model = self._model
            if model is None:
                model = self._load_model()
                self._model = model
            result = model.predict(
                frame,
                classes=[_SPORTS_BALL_CLASS],
                conf=self._settings.ball_confidence,
                iou=self._settings.ball_iou,
                imgsz=self._settings.ball_image_size,
                device=self._settings.model_device,
                verbose=False,
            )[0]
            detected: list[BallDetection] = []
            for box, confidence in zip(
                result.boxes.xyxy.cpu().tolist(), result.boxes.conf.cpu().tolist(), strict=True
            ):
                x1, y1, x2, y2 = map(float, box)
                if not np.isfinite([x1, y1, x2, y2, confidence]).all() or x2 <= x1 or y2 <= y1:
                    continue
                detected.append(
                    BallDetection(
                        frame_index,
                        timestamp_seconds,
                        float(confidence),
                        BoundingBox(x1, y1, x2, y2),
                        ((x1 + x2) / 2, (y1 + y2) / 2),
                    )
                )
            self._logger.info(
                "ball_inference frame=%d detections=%d elapsed_ms=%d",
                frame_index,
                len(detected),
                round((perf_counter() - started) * 1000),
            )
            return tuple(detected)
        except BallDetectionError:
            raise
        except Exception as error:
            raise BallDetectionError(f"Ball detector inference failed: {error}") from error
