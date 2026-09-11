"""Module #4 (AI/ML Engine) Integration Interface and Mock Adapter.

Represents the computer vision and deep learning layer responsible for object
detection (YOLO/RT-DETR), multi-object tracking (ByteTrack/DeepOCSORT), and
person/vehicle re-identification (ReID) embeddings.
"""

import random
from abc import ABC, abstractmethod
from datetime import datetime

from src.timeline.schemas import AIDetectionPayload, BoundingBox


class AIEngineInterface(ABC):
    """Integration contract that Module #4 (AI/ML Engine) will fulfill."""

    @abstractmethod
    async def process_frame(
        self,
        case_id: str,
        evidence_id: str,
        channel_id: str,
        frame_index: int,
        utc_timestamp: datetime,
    ) -> list[AIDetectionPayload]:
        """Perform object detection and ReID feature extraction on a video frame."""
        pass


class MockAIEngine(AIEngineInterface):
    """Mock adapter simulating AI object detection and ReID feature extraction."""

    def __init__(self, seed: int = 42) -> None:
        self.rng = random.Random(seed)

    async def process_frame(
        self,
        case_id: str,
        evidence_id: str,
        channel_id: str,
        frame_index: int,
        utc_timestamp: datetime,
    ) -> list[AIDetectionPayload]:
        """Generate synthetic person/vehicle detection with optional mock ReID vector."""
        classes = ["person", "car", "motorcycle", "backpack"]
        chosen_class = self.rng.choice(classes)
        conf = round(self.rng.uniform(0.78, 0.98), 3)

        # Correction #2: track_id is camera-local by default!
        local_track_id = self.rng.randint(1, 20)

        # Generate a synthetic 128-dimensional unit vector embedding
        raw_vec = [self.rng.gauss(0, 1) for _ in range(128)]
        magnitude = sum(x * x for x in raw_vec) ** 0.5
        normalized_embedding = [round(x / magnitude, 4) for x in raw_vec]

        return [
            AIDetectionPayload(
                case_id=case_id,
                evidence_id=evidence_id,
                channel_id=channel_id,
                utc_timestamp=utc_timestamp,
                frame_index=frame_index,
                object_class=chosen_class,
                confidence=conf,
                bounding_box=BoundingBox(
                    xmin=round(self.rng.uniform(0.1, 0.4), 3),
                    ymin=round(self.rng.uniform(0.2, 0.5), 3),
                    xmax=round(self.rng.uniform(0.5, 0.8), 3),
                    ymax=round(self.rng.uniform(0.6, 0.9), 3),
                ),
                track_id=local_track_id,
                is_global_track_id=False,  # Camera-local by default (Correction #2)
                embedding=normalized_embedding,
                detection_metadata={"detector_model": "YOLOv10x", "reid_model": "OSNet-x1_0"},
            )
        ]
