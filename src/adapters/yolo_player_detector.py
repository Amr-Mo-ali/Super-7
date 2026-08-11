"""Ultralytics YOLO person detector adapter."""

import logging
from collections.abc import Sequence
from time import perf_counter
from typing import Any

import numpy as np

from core.config import Settings
from core.exceptions import InferenceError, InvalidFrameError, ModelLoadingError
from services.player_detector import BoundingBox, Detection, PlayerDetectorProtocol


class YOLOPlayerDetector(PlayerDetectorProtocol):
    """Loads one YOLO model and converts only COCO person detections."""

    def __init__(
        self, settings: Settings, logger: logging.Logger, model: Any | None = None
    ) -> None:
        self._settings = settings
        self._logger = logger
        self._model = model

    def _load_model(self) -> Any:
        started = perf_counter()
        try:
            from ultralytics import YOLO  # type: ignore[attr-defined]

            model = YOLO(self._settings.model_path)
            self._logger.info(
                "model_loaded path=%s device=%s elapsed_ms=%d",
                self._settings.model_path,
                self._settings.model_device,
                round((perf_counter() - started) * 1000),
            )
            return model
        except Exception as error:
            raise ModelLoadingError(f"Unable to load player detector: {error}") from error

    def detect(
        self, frame: np.ndarray, frame_index: int = 0, timestamp: float = 0.0
    ) -> Sequence[Detection]:
        """Detect COCO class 0 persons in one valid BGR frame."""
        self._validate_frame(frame)
        started = perf_counter()
        try:
            model = self._model
            if model is None:
                model = self._load_model()
                self._model = model
            result = model.predict(
                frame,
                classes=[0],
                conf=self._settings.model_confidence,
                iou=self._settings.model_iou,
                imgsz=self._settings.model_image_size,
                device=self._settings.model_device,
                verbose=False,
            )[0]
            detections = tuple(
                Detection(
                    None,
                    "person",
                    float(confidence),
                    BoundingBox(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                    frame_index,
                    timestamp,
                )
                for box, confidence in zip(
                    result.boxes.xyxy.cpu().tolist(), result.boxes.conf.cpu().tolist(), strict=True
                )
            )
            self._logger.info(
                "player_inference frame=%d detections=%d elapsed_ms=%d",
                frame_index,
                len(detections),
                round((perf_counter() - started) * 1000),
            )
            return detections
        except Exception as error:
            raise InferenceError(f"Player detector inference failed: {error}") from error

    def detect_batch(self, frames: Sequence[np.ndarray]) -> Sequence[Sequence[Detection]]:
        """Run batched person inference while retaining per-frame output ordering."""
        return tuple(self.detect(frame, index, float(index)) for index, frame in enumerate(frames))

    @staticmethod
    def _validate_frame(frame: np.ndarray) -> None:
        if (
            not isinstance(frame, np.ndarray)
            or frame.size == 0
            or frame.ndim != 3
            or frame.shape[2] != 3
        ):
            raise InvalidFrameError("Detector frame must be a non-empty three-channel image.")
