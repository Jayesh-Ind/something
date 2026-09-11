"""Module #1 (Core Engine — C++) Integration Interface and Mock Adapter.

Represents the physical acquisition layer responsible for raw disk reading,
device identification, and cryptographic hashing.
"""

import hashlib
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from src.timeline.constants import TimestampFormat, TimestampSource, VendorType
from src.timeline.schemas import RawFrameMeta


class CoreEngineInterface(ABC):
    """Integration contract that Module #1 (C++ Core Engine) will fulfill."""

    @abstractmethod
    async def identify_device(self, evidence_path: str) -> dict[str, Any]:
        """Inspect raw evidence image and return vendor hardware metadata."""
        pass

    @abstractmethod
    async def stream_frames(
        self, case_id: str, evidence_id: str, channel_id: str, count: int = 10
    ) -> AsyncGenerator[RawFrameMeta, None]:
        """Stream raw acquired frame metadata and cryptographic hashes."""
        pass


class MockCoreEngine(CoreEngineInterface):
    """Mock adapter simulating C++ Core Engine acquisition."""

    def __init__(self, vendor: VendorType = VendorType.HIKVISION) -> None:
        self.vendor = vendor

    async def identify_device(self, evidence_path: str) -> dict[str, Any]:
        return {
            "evidence_path": evidence_path,
            "detected_vendor": self.vendor.value,
            "filesystem": "FAT32_PROPRIETARY",
            "sector_size": 512,
            "total_channels": 8,
            "device_model": f"{self.vendor.value}-NVR7000-4K",
            "firmware_version": "v4.32.010_build240115",
            "evidence_hash_sha256": hashlib.sha256(evidence_path.encode()).hexdigest(),
        }

    async def stream_frames(
        self, case_id: str, evidence_id: str, channel_id: str, count: int = 10
    ) -> AsyncGenerator[RawFrameMeta, None]:
        """Yield synthetic raw frame metadata blocks."""
        for idx in range(count):
            frame_offset = idx * 65536
            frame_hash = hashlib.sha256(f"frame_content_{channel_id}_{idx}".encode()).hexdigest()

            # Emulate vendor timestamp
            raw_ts = f"2026-03-10 12:00:{idx:02d}"
            fmt = TimestampFormat.VENDOR_DAHUA
            if self.vendor == VendorType.HIKVISION:
                raw_ts = f"20260310T1200{idx:02d}Z"
                fmt = TimestampFormat.VENDOR_HIKVISION

            yield RawFrameMeta(
                case_id=case_id,
                evidence_id=evidence_id,
                channel_id=channel_id,
                vendor_type=self.vendor,
                raw_timestamp_str=raw_ts,
                timestamp_format=fmt,
                timestamp_source=TimestampSource.RECORDING_EMBEDDED,
                frame_index=idx,
                file_offset_bytes=frame_offset,
                pts=idx * 1000,
                dts=idx * 1000,
                file_path=f"/evidence/disk01/{channel_id}.raw",
                frame_hash_sha256=frame_hash,
            )
