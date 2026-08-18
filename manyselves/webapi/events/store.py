"""SQLite-based event log persistence with filtering.

Replaces the in-memory ReplayBuffer for long-term event storage.
Filters out high-frequency streaming events to reduce noise.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from loguru import logger

from .models import EventEnvelope
from .sanitizer import EventPayloadSanitizer


# Events that should NOT be persisted to the database (too frequent/noisy)
SKIPPED_EVENT_TYPES = {
    "agent.message.delta",  # Streaming chunks - only save the completed message
    "agent.thinking.delta",  # Thinking chunks - only save the snapshot
}
_LOG_MESSAGE_FIELDS = (
    "message",
    "content",
    "brief",
    "description",
    "status",
    "action",
    "reason",
    "name",
    "tool_name",
)
_LOG_MESSAGE_LIMIT = 500


def _event_level(event: EventEnvelope) -> str:
    event_type = event.type.casefold()
    status_value = event.payload.get("status")
    payload_status = status_value.casefold() if isinstance(status_value, str) else ""
    if "error" in event_type or "failed" in event_type or payload_status in {"error", "failed"}:
        return "error"
    if any(marker in event_type for marker in ("warning", "waiting", "interrupt", "cancel")):
        return "warning"
    return "info"


def _event_message(event: EventEnvelope) -> str:
    sanitized = EventPayloadSanitizer().sanitize_mapping(event.payload)
    for field in _LOG_MESSAGE_FIELDS:
        value = sanitized.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_LOG_MESSAGE_LIMIT]
    return event.type


class EventStore:
    """SQLite-backed event log with indexing and filtering.

    Layout:
        .manyselves/events.db
        └── events table with indexes
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_tables()
        logger.info(f"EventStore initialized at {self.db_path}")

    def _init_tables(self) -> None:
        """Create tables and indexes."""
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    project_id TEXT NOT NULL,
                    agent_id TEXT,
                    session_id TEXT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT,
                    message_id TEXT,
                    payload TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            # Indexes for common queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_project_timestamp
                ON events(project_id, timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_project_type
                ON events(project_id, event_type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_project_level
                ON events(project_id, level)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_project_agent
                ON events(project_id, agent_id)
            """)

            conn.commit()

    def append(self, event: EventEnvelope) -> None:
        """Append an event to the log, skipping noisy streaming events."""
        # Skip high-frequency streaming events
        if event.type in SKIPPED_EVENT_TYPES:
            return
        if event.project_id is None:
            return

        level = _event_level(event)
        message = _event_message(event)

        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO events (
                    event_id, stream_id, sequence, project_id, agent_id, session_id,
                    timestamp, event_type, level, message, message_id, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.event_id,
                event.stream_id,
                event.sequence,
                event.project_id,
                event.agent_id,
                event.session_id,
                event.timestamp.isoformat(),
                event.type,
                level,
                message,
                event.message_id,
                json.dumps(event.payload) if event.payload else None,
                datetime.now(UTC).isoformat(),
            ))
            conn.commit()

    def list(
        self,
        project_id: str,
        limit: int = 100,
        offset: int = 0,
        level: str | None = None,
        event_type: str | None = None,
        agent_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """List events with pagination and filtering."""
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # Build query
            conditions = ["project_id = ?"]
            params = [project_id]

            if level:
                conditions.append("level = ?")
                params.append(level)
            if event_type:
                conditions.append("event_type = ?")
                params.append(event_type)
            if agent_id:
                conditions.append("agent_id = ?")
                params.append(agent_id)

            where_clause = " AND ".join(conditions)

            # Get total count
            cursor = conn.execute(
                f"SELECT COUNT(*) FROM events WHERE {where_clause}",
                params,
            )
            total = cursor.fetchone()[0]

            # Get events
            cursor = conn.execute(
                f"""
                SELECT * FROM events
                WHERE {where_clause}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )

            events = []
            for row in cursor:
                events.append({
                    "eventId": row["event_id"],
                    "streamId": row["stream_id"],
                    "sequence": row["sequence"],
                    "projectId": row["project_id"],
                    "agentId": row["agent_id"],
                    "sessionId": row["session_id"],
                    "timestamp": row["timestamp"],
                    "type": row["event_type"],
                    "level": row["level"],
                    "message": row["message"],
                    "messageId": row["message_id"],
                    "payload": json.loads(row["payload"]) if row["payload"] else None,
                })

            return events, total

    def search(
        self,
        project_id: str,
        query: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search events by message content."""
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row

            cursor = conn.execute(
                """
                SELECT * FROM events
                WHERE project_id = ? AND message LIKE ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (project_id, f"%{query}%", limit),
            )

            events = []
            for row in cursor:
                events.append({
                    "eventId": row["event_id"],
                    "projectId": row["project_id"],
                    "agentId": row["agent_id"],
                    "timestamp": row["timestamp"],
                    "type": row["event_type"],
                    "level": row["level"],
                    "message": row["message"],
                })

            return events

    def stats(self, project_id: str) -> dict[str, Any]:
        """Get event statistics for a project."""
        with self._lock, sqlite3.connect(str(self.db_path)) as conn:

            # Count by level
            cursor = conn.execute(
                """
                SELECT level, COUNT(*) as count
                FROM events
                WHERE project_id = ?
                GROUP BY level
                """,
                (project_id,),
            )
            by_level = {row[0]: row[1] for row in cursor}

            # Count by type
            cursor = conn.execute(
                """
                SELECT event_type, COUNT(*) as count
                FROM events
                WHERE project_id = ?
                GROUP BY event_type
                ORDER BY count DESC
                LIMIT 10
                """,
                (project_id,),
            )
            by_type = {row[0]: row[1] for row in cursor}

            # Total count
            cursor = conn.execute(
                "SELECT COUNT(*) FROM events WHERE project_id = ?",
                (project_id,),
            )
            total = cursor.fetchone()[0]

            # Date range
            cursor = conn.execute(
                """
                SELECT MIN(timestamp), MAX(timestamp)
                FROM events
                WHERE project_id = ?
                """,
                (project_id,),
            )
            row = cursor.fetchone()
            date_range = {
                "start": row[0],
                "end": row[1],
            }

            return {
                "total": total,
                "byLevel": by_level,
                "byType": by_type,
                "dateRange": date_range,
            }

    def cleanup(self, days: int = 30) -> int:
        """Delete events older than N days."""
        cutoff = datetime.now(UTC).timestamp() - (days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff, UTC).isoformat()

        with self._lock, sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute(
                "DELETE FROM events WHERE timestamp < ?",
                (cutoff_iso,),
            )
            deleted = cursor.rowcount
            conn.commit()

        logger.info(f"Cleaned up {deleted} events older than {days} days")
        return deleted

    def close(self) -> None:
        """Close the database connection."""
        # SQLite connections are closed automatically
        pass
