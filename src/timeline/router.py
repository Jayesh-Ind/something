"""FastAPI REST API routes and WebSocket live timeline streaming."""

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from src.timeline.constants import EventType
from src.timeline.dependencies import PipelineDep, TimelineServiceDep
from src.timeline.exceptions import (
    DuplicateEventError,
    EntityNotFoundError,
    PipelineQueueFullError,
)
from src.timeline.normalizer import TimelineNormalizer
from src.timeline.schemas import (
    AIDetectionPayload,
    CorrelatedEvent,
    CorrelationRequest,
    CorrelationResponse,
    NormalizationRequest,
    NormalizationResponse,
    PipelineStatus,
    RawFrameMeta,
    TimelineEvent,
    TimelineExportResponse,
    TimelineQuery,
    TimelineResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Forensic Timeline"])


# ============================================================================
# Ingestion Endpoints
# ============================================================================


@router.post(
    "/ingest/frame-metadata",
    status_code=status.HTTP_201_CREATED,
    summary="Ingest raw frame metadata",
    description="Accepts raw metadata from Core or Codec engine for asynchronous normalization and timeline ordering.",
    responses={
        status.HTTP_201_CREATED: {"description": "Frame metadata queued successfully"},
        status.HTTP_409_CONFLICT: {"description": "Duplicate frame already processed"},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Pipeline queue is full (backpressure)"},
    },
)
async def ingest_frame_metadata(
    payload: RawFrameMeta,
    pipeline: PipelineDep,
) -> dict[str, Any]:
    try:
        dedup_id = await pipeline.submit_raw_frame(payload)
        return {
            "status": "queued",
            "message": "Raw frame metadata submitted for async normalization",
            "dedup_id": dedup_id,
        }
    except PipelineQueueFullError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.message,
        ) from exc
    except DuplicateEventError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.message,
        ) from exc


@router.post(
    "/ingest/ai-detection",
    status_code=status.HTTP_201_CREATED,
    summary="Ingest AI detection payload",
    description="Accepts object detections, tracks, and optional ReID embeddings from Module #4 for timeline integration.",
    responses={
        status.HTTP_201_CREATED: {"description": "AI detection queued successfully"},
        status.HTTP_409_CONFLICT: {"description": "Duplicate detection already processed"},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Pipeline queue is full (backpressure)"},
    },
)
async def ingest_ai_detection(
    payload: AIDetectionPayload,
    pipeline: PipelineDep,
) -> dict[str, Any]:
    try:
        dedup_id = await pipeline.submit_ai_detection(payload)
        return {
            "status": "queued",
            "message": "AI detection submitted for async correlation and timeline storage",
            "dedup_id": dedup_id,
        }
    except PipelineQueueFullError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.message,
        ) from exc
    except DuplicateEventError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.message,
        ) from exc


# ============================================================================
# Normalization & Correlation Standalone Endpoints
# ============================================================================


@router.post(
    "/timestamps/normalize",
    status_code=status.HTTP_200_OK,
    summary="Standalone timestamp normalization",
    description="Normalizes a single raw timestamp without mutating source or requiring prior case creation.",
)
async def normalize_timestamp(
    payload: NormalizationRequest,
) -> NormalizationResponse:
    normalizer = TimelineNormalizer()
    res = normalizer.normalize(
        raw_timestamp_str=payload.raw_timestamp_str,
        timestamp_format=payload.timestamp_format,
        timestamp_source=payload.timestamp_source,
        supplied_timezone=payload.timezone,
        offset_ms=payload.channel_offset_ms,
        drift_rate_ppm=payload.drift_rate_ppm,
        pts=payload.pts,
        time_base_num=payload.time_base_num,
        time_base_den=payload.time_base_den,
    )
    return NormalizationResponse(
        success=res.success,
        raw_timestamp=res.raw_timestamp,
        utc_timestamp=res.utc_timestamp,
        applied_offset_ms=res.applied_offset_ms,
        timestamp_source=res.timestamp_source,
        detected_format=res.detected_format,
        anomaly_flags=res.anomaly_flags,
        errors=res.errors,
    )


@router.post(
    "/events/correlate",
    status_code=status.HTTP_200_OK,
    summary="Trigger cross-camera correlation",
    description="Runs cross-camera temporal sliding window correlation across stored events for a case.",
)
async def correlate_case_events(
    payload: CorrelationRequest,
    service: TimelineServiceDep,
    pipeline: PipelineDep,
) -> CorrelationResponse:
    # Query case events
    query = TimelineQuery(
        case_id=payload.case_id,
        limit=1000,
    )
    resp = await service.query_timeline(query)
    correlations = pipeline.correlator.correlate_batch(
        resp.events, custom_window_seconds=payload.window_seconds
    )
    return CorrelationResponse(
        case_id=payload.case_id,
        correlated_count=len(correlations),
        correlations=correlations,
    )


# ============================================================================
# Timeline Query & Retrieval Endpoints
# ============================================================================


@router.get(
    "/timeline/{case_id}",
    status_code=status.HTTP_200_OK,
    summary="Retrieve unified case timeline",
    description="Returns chronologically ordered events across all cameras for a forensic case.",
)
async def get_unified_timeline(
    case_id: str,
    service: TimelineServiceDep,
    channel_id: str | None = Query(None, description="Filter by specific camera"),
    event_types: list[EventType] | None = Query(None, description="Filter by event types"),
    start_time: datetime | None = Query(None, description="Start time filter (UTC)"),
    end_time: datetime | None = Query(None, description="End time filter (UTC)"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> TimelineResponse:
    query = TimelineQuery(
        case_id=case_id,
        channel_id=channel_id,
        event_types=event_types,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )
    return await service.query_timeline(query)


@router.get(
    "/timeline/{case_id}/export",
    status_code=status.HTTP_200_OK,
    summary="Export forensic timeline package",
    description="Generates a court-admissible forensic export bundle including provenance, corrections, and integrity statements.",
)
async def export_case_timeline(
    case_id: str,
    service: TimelineServiceDep,
) -> TimelineExportResponse:
    return await service.export_timeline(case_id)


@router.get(
    "/timeline/{case_id}/{channel_id}",
    status_code=status.HTTP_200_OK,
    summary="Retrieve camera-specific timeline",
    description="Returns chronologically ordered events isolated to a specific camera channel.",
)
async def get_channel_timeline(
    case_id: str,
    channel_id: str,
    service: TimelineServiceDep,
    event_types: list[EventType] | None = Query(None),
    start_time: datetime | None = Query(None),
    end_time: datetime | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> TimelineResponse:
    query = TimelineQuery(
        case_id=case_id,
        channel_id=channel_id,
        event_types=event_types,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )
    return await service.query_timeline(query)


@router.get(
    "/events/{event_id}",
    status_code=status.HTTP_200_OK,
    summary="Retrieve single timeline event",
    description="Fetches full forensic details, source references, and anomalies of an event.",
)
async def get_event_by_id(
    event_id: str,
    service: TimelineServiceDep,
) -> TimelineEvent:
    try:
        return await service.get_event_by_id(event_id)
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=exc.message,
        ) from exc


@router.get(
    "/correlations/{case_id}",
    status_code=status.HTTP_200_OK,
    summary="Retrieve case cross-camera correlations",
    description="Returns all recorded cross-camera correlations discovered for the case.",
)
async def get_case_correlations(
    case_id: str,
    service: TimelineServiceDep,
) -> list[CorrelatedEvent]:
    return await service.get_correlations_by_case(case_id)


@router.get(
    "/pipeline/status",
    status_code=status.HTTP_200_OK,
    summary="Query pipeline ingestion metrics",
    description="Returns current queue pressure, throughput, and anomaly counters.",
)
async def get_pipeline_metrics(
    pipeline: PipelineDep,
) -> PipelineStatus:
    return pipeline.get_status()


# ============================================================================
# WebSocket Connection Manager & Live Endpoint
# ============================================================================


class WebSocketConnectionManager:
    """Manages active live WebSocket subscribers."""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info("WebSocket client connected. Total clients: %d", len(self.active_connections))

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(
            "WebSocket client disconnected. Remaining clients: %d", len(self.active_connections)
        )

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send message to all connected clients."""
        text = json.dumps(message)
        dead_connections: list[WebSocket] = []
        for connection in self.active_connections:
            try:
                await connection.send_text(text)
            except Exception:
                dead_connections.append(connection)

        for dead in dead_connections:
            self.disconnect(dead)


ws_manager = WebSocketConnectionManager()
ws_router = APIRouter(tags=["WebSocket"])


@ws_router.websocket("/ws/live-timeline")
async def websocket_live_timeline(websocket: WebSocket) -> None:
    """Live WebSocket feed streaming newly generated timeline events and correlations."""
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep-alive heartbeat / listen for client ping or filter requests
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as exc:
        logger.warning("WebSocket error: %s", exc)
        ws_manager.disconnect(websocket)
