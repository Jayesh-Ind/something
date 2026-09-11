"""Async Pipeline Orchestrator.

Manages bounded asynchronous ingestion queues, normalization, deduplication,
persistence, cross-camera correlation, and event broadcasting.
Decoupled completely from HTTP transport semantics.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import SessionFactory
from src.timeline.constants import (
    EventType,
    TimestampSource,
)
from src.timeline.correlator import EventCorrelator
from src.timeline.exceptions import DuplicateEventError, PipelineQueueFullError
from src.timeline.models import CorrelatedEventModel, TimelineEventModel
from src.timeline.normalizer import TimelineNormalizer
from src.timeline.schemas import (
    AIDetectionPayload,
    CorrelatedEvent,
    PipelineStatus,
    RawFrameMeta,
    TimelineEvent,
)

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    """Core orchestration service for Module #3."""

    def __init__(
        self,
        normalizer: TimelineNormalizer | None = None,
        correlator: EventCorrelator | None = None,
        queue_max_size: int | None = None,
    ) -> None:
        self.normalizer = normalizer or TimelineNormalizer()
        self.correlator = correlator or EventCorrelator()
        self.queue_max_size = queue_max_size or settings.PIPELINE_QUEUE_MAX_SIZE

        # Bounded asynchronous ingestion queue
        self._queue: asyncio.Queue[RawFrameMeta | AIDetectionPayload | TimelineEvent] = (
            asyncio.Queue(maxsize=self.queue_max_size)
        )

        # Worker lifecycle
        self._worker_task: asyncio.Task | None = None
        self._is_running = False

        # Metrics & Deduplication
        self._processed_count = 0
        self._dropped_count = 0
        self._anomalies_count = 0
        self._dedup_cache: set[str] = set()

        # Track last seen timestamp & frame index per channel for anomaly detection
        self._channel_state: dict[str, tuple[datetime, int]] = {}

        # Broadcast listener callbacks (e.g. WebSocket connection manager)
        self._broadcast_listeners: list[Callable[[dict[str, Any]], asyncio.Future | None]] = []

    def register_broadcast_listener(self, listener: Callable[[dict[str, Any]], Any]) -> None:
        """Register a callback to receive live pipeline events (WebSocket bus)."""
        self._broadcast_listeners.append(listener)

    async def submit_raw_frame(self, frame_meta: RawFrameMeta) -> str:
        """Ingest raw frame metadata into the async queue.

        Raises PipelineQueueFullError if queue capacity is exceeded.
        """
        dedup_key = f"frame:{frame_meta.case_id}:{frame_meta.channel_id}:{frame_meta.frame_index}:{frame_meta.raw_timestamp_str}"
        if dedup_key in self._dedup_cache:
            raise DuplicateEventError(
                f"Duplicate frame index {frame_meta.frame_index} for channel {frame_meta.channel_id}"
            )

        self._record_dedup_key(dedup_key)

        try:
            self._queue.put_nowait(frame_meta)
            return dedup_key
        except asyncio.QueueFull as exc:
            self._dropped_count += 1
            raise PipelineQueueFullError(
                queue_size=self._queue.qsize(), max_size=self.queue_max_size
            ) from exc

    async def submit_ai_detection(self, ai_payload: AIDetectionPayload) -> str:
        """Ingest AI detection payload into the async queue.

        Raises PipelineQueueFullError if queue capacity is exceeded.
        """
        dedup_key = f"ai:{ai_payload.case_id}:{ai_payload.channel_id}:{ai_payload.frame_index}:{ai_payload.object_class}:{ai_payload.track_id}"
        if dedup_key in self._dedup_cache:
            raise DuplicateEventError(f"Duplicate AI detection for channel {ai_payload.channel_id}")

        self._record_dedup_key(dedup_key)

        try:
            self._queue.put_nowait(ai_payload)
            return dedup_key
        except asyncio.QueueFull as exc:
            self._dropped_count += 1
            raise PipelineQueueFullError(
                queue_size=self._queue.qsize(), max_size=self.queue_max_size
            ) from exc

    def _record_dedup_key(self, key: str) -> None:
        """Maintain bounded deduplication cache."""
        self._dedup_cache.add(key)
        if len(self._dedup_cache) > settings.DEDUPLICATION_CACHE_SIZE:
            # Evict half
            self._dedup_cache = set(list(self._dedup_cache)[-5000:])

    async def start(self) -> None:
        """Start the background ingestion pipeline worker."""
        if self._is_running:
            return
        self._is_running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("PipelineOrchestrator worker started (queue capacity: %d)", self.queue_max_size)

    async def stop(self) -> None:
        """Gracefully stop worker and drain pending queue items."""
        if not self._is_running:
            return
        self._is_running = False
        logger.info("Stopping PipelineOrchestrator worker. Draining pending items...")

        # Drain pending queue up to timeout
        try:
            await asyncio.wait_for(
                self._queue.join(), timeout=settings.PIPELINE_DRAIN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.warning("Pipeline drain timed out; proceeding with shutdown.")

        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("PipelineOrchestrator worker stopped cleanly.")

    async def _worker_loop(self) -> None:
        """Continuously process incoming items from the queue."""
        while self._is_running:
            try:
                item = await self._queue.get()
                await self._process_pipeline_item(item)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error processing pipeline item: %s", exc, exc_info=True)
                self._dropped_count += 1

    async def _process_pipeline_item(
        self, item: RawFrameMeta | AIDetectionPayload | TimelineEvent
    ) -> None:
        """Normalize, check continuity anomalies, correlate, persist, and broadcast."""
        event: TimelineEvent | None = None

        if isinstance(item, RawFrameMeta):
            event = self._convert_raw_frame_to_event(item)
        elif isinstance(item, AIDetectionPayload):
            event = self._convert_ai_payload_to_event(item)
        elif isinstance(item, TimelineEvent):
            event = item

        if event is None:
            return

        # Check temporal continuity anomalies (time jumps, timeline gaps)
        ch_state = self._channel_state.get(event.channel_id)
        if ch_state:
            last_time, last_idx = ch_state
            anomalies = TimelineNormalizer.check_temporal_continuity(
                channel_id=event.channel_id,
                current_utc=event.utc_timestamp,
                current_frame_index=event.frame_index or 0,
                last_utc=last_time,
                last_frame_index=last_idx,
            )
            if anomalies:
                event.anomaly_flags.extend(anomalies)
                self._anomalies_count += len(anomalies)
                # Broadcast anomaly event
                await self._broadcast(
                    {
                        "type": "timeline_anomaly",
                        "data": {
                            "channel_id": event.channel_id,
                            "anomalies": anomalies,
                            "event_id": event.event_id,
                            "utc_timestamp": event.utc_timestamp.isoformat(),
                        },
                    }
                )

        self._channel_state[event.channel_id] = (
            event.utc_timestamp,
            event.frame_index or 0,
        )

        # Correlate across cameras
        correlations = await self.correlator.add_event(event)

        # Persist event and any correlations in database
        async with SessionFactory() as session:
            try:
                await self._persist_event(session, event)
                for corr in correlations:
                    await self._persist_correlation(session, corr)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.error("Failed to persist event %s: %s", event.event_id, exc)

        self._processed_count += 1

        # Broadcast live timeline event to WebSocket listeners
        await self._broadcast(
            {
                "type": "timeline_event",
                "data": event.model_dump(mode="json"),
            }
        )

        # Broadcast correlated events
        for corr in correlations:
            await self._broadcast(
                {
                    "type": "correlated_event",
                    "data": corr.model_dump(mode="json"),
                }
            )

    def _convert_raw_frame_to_event(self, raw: RawFrameMeta) -> TimelineEvent | None:
        """Normalize raw frame metadata to canonical TimelineEvent."""
        norm = self.normalizer.normalize(
            raw_timestamp_str=raw.raw_timestamp_str,
            timestamp_format=raw.timestamp_format,
            timestamp_source=raw.timestamp_source,
            supplied_timezone=raw.timezone,
            pts=raw.pts,
            time_base_num=raw.time_base_num,
            time_base_den=raw.time_base_den,
        )

        if not norm.success or norm.utc_timestamp is None:
            logger.warning(
                "Failed to normalize raw frame: %s (Errors: %s)", raw.raw_timestamp_str, norm.errors
            )
            self._anomalies_count += 1
            return None

        return TimelineEvent(
            event_id=str(uuid4()),
            case_id=raw.case_id,
            evidence_id=raw.evidence_id,
            channel_id=raw.channel_id,
            utc_timestamp=norm.utc_timestamp,
            raw_timestamp=raw.raw_timestamp_str,
            timestamp_source=norm.timestamp_source,
            applied_offset_ms=norm.applied_offset_ms,
            event_type=EventType.FRAME_INDEX,
            frame_index=raw.frame_index,
            file_offset_bytes=raw.file_offset_bytes,
            pts=raw.pts,
            dts=raw.dts,
            payload={
                "vendor_type": raw.vendor_type.value,
                "frame_hash_sha256": raw.frame_hash_sha256,
                "file_path": raw.file_path,
                "time_base_num": raw.time_base_num,
                "time_base_den": raw.time_base_den,
            },
            source_reference={
                "evidence_id": raw.evidence_id,
                "file_offset_bytes": raw.file_offset_bytes,
                "frame_index": raw.frame_index,
                "pts": raw.pts,
                "time_base_num": raw.time_base_num,
                "time_base_den": raw.time_base_den,
            },
            anomaly_flags=norm.anomaly_flags,
        )

    def _convert_ai_payload_to_event(self, ai: AIDetectionPayload) -> TimelineEvent | None:
        """Convert AI detection payload to canonical TimelineEvent."""
        utc_time = ai.utc_timestamp
        raw_str = ai.raw_timestamp_str or (utc_time.isoformat() if utc_time else "")
        applied_offset = 0.0
        anomalies: list[str] = []

        if utc_time is None:
            norm = self.normalizer.normalize(
                raw_timestamp_str=raw_str,
                timestamp_source=TimestampSource.RECORDING_EMBEDDED,
            )
            if not norm.success or norm.utc_timestamp is None:
                logger.warning("Failed to normalize AI timestamp: %s", raw_str)
                return None
            utc_time = norm.utc_timestamp
            applied_offset = norm.applied_offset_ms
            anomalies.extend(norm.anomaly_flags)

        return TimelineEvent(
            event_id=str(uuid4()),
            case_id=ai.case_id,
            evidence_id=ai.evidence_id,
            channel_id=ai.channel_id,
            utc_timestamp=utc_time,
            raw_timestamp=raw_str,
            timestamp_source=TimestampSource.RECORDING_EMBEDDED,
            applied_offset_ms=applied_offset,
            event_type=EventType.AI_DETECTION,
            frame_index=ai.frame_index,
            payload={
                "object_class": ai.object_class,
                "confidence": ai.confidence,
                "bounding_box": ai.bounding_box.model_dump() if ai.bounding_box else None,
                "track_id": str(ai.track_id) if ai.track_id is not None else None,
                "is_global_track_id": ai.is_global_track_id,
                "embedding": ai.embedding,
                "detection_metadata": ai.detection_metadata,
            },
            source_reference={
                "evidence_id": ai.evidence_id,
                "frame_index": ai.frame_index,
            },
            anomaly_flags=anomalies,
        )

    async def _persist_event(self, session: AsyncSession, event: TimelineEvent) -> None:
        """Save TimelineEventModel to database."""
        db_event = TimelineEventModel(
            id=event.event_id,
            case_id=event.case_id,
            evidence_id=event.evidence_id,
            channel_id=event.channel_id,
            utc_timestamp=event.utc_timestamp,
            raw_timestamp=event.raw_timestamp,
            timestamp_source=event.timestamp_source.value,
            applied_offset_ms=event.applied_offset_ms,
            event_type=event.event_type.value,
            frame_index=event.frame_index,
            file_offset_bytes=event.file_offset_bytes,
            pts=event.pts,
            dts=event.dts,
            payload_json=json.dumps(event.payload),
            source_reference_json=json.dumps(event.source_reference),
            anomaly_flags_json=json.dumps(event.anomaly_flags),
            created_at=datetime.now(UTC),
        )
        session.add(db_event)

    async def _persist_correlation(self, session: AsyncSession, corr: CorrelatedEvent) -> None:
        """Save CorrelatedEventModel to database."""
        db_corr = CorrelatedEventModel(
            id=str(uuid4()),
            correlation_id=corr.correlation_id,
            case_id=corr.case_id,
            primary_channel=corr.primary_channel,
            secondary_channels_json=json.dumps(corr.secondary_channels),
            source_event_ids_json=json.dumps(corr.source_event_ids),
            start_time_utc=corr.start_time_utc,
            end_time_utc=corr.end_time_utc,
            event_type=corr.event_type,
            correlation_type=corr.correlation_type.value,
            confidence=corr.confidence,
            explanation=corr.explanation,
            metadata_json=json.dumps(corr.metadata),
            created_at=datetime.now(UTC),
        )
        session.add(db_corr)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        """Dispatch message to registered WebSocket listeners."""
        for listener in self._broadcast_listeners:
            try:
                res = listener(message)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.error("Error broadcasting message: %s", exc)

    def get_status(self) -> PipelineStatus:
        """Query current pipeline operational metrics."""
        return PipelineStatus(
            queue_size=self._queue.qsize(),
            queue_max_size=self.queue_max_size,
            is_healthy=self._is_running,
            processed_events_count=self._processed_count,
            dropped_events_count=self._dropped_count,
            detected_anomalies_count=self._anomalies_count,
        )


# Global singleton instance for application lifecycle
pipeline_instance = PipelineOrchestrator()
