from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Dict, Iterable, List, Optional

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import Client
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.base_document import DocumentSnapshot
from google.cloud.firestore_v1._helpers import GeoPoint


class FirestoreBrowserError(Exception):
    """Raised when the browser app cannot perform a Firestore action."""


@dataclass
class ConnectionConfig:
    credentials_path: str
    project_id: str = ""


def _app_name(config: ConnectionConfig) -> str:
    return f"firemyadmin::{config.credentials_path}::{config.project_id}"


def get_client(config: ConnectionConfig) -> Client:
    if not config.credentials_path:
        raise FirestoreBrowserError("Please provide a Firebase Admin SDK JSON key path.")

    name = _app_name(config)
    try:
        app = firebase_admin.get_app(name)
    except ValueError:
        cred = credentials.Certificate(config.credentials_path)
        options = {"projectId": config.project_id} if config.project_id else None
        app = firebase_admin.initialize_app(cred, options=options, name=name)
    return firestore.client(app=app)


def list_root_collections(client: Client) -> List[str]:
    return sorted(collection.id for collection in client.collections())


def list_subcollections(document_ref) -> List[str]:
    return sorted(collection.id for collection in document_ref.collections())


def get_document(client: Client, document_path: str) -> DocumentSnapshot:
    snapshot = client.document(document_path).get()
    if not snapshot.exists:
        raise FirestoreBrowserError(f"Document not found: {document_path}")
    return snapshot


def get_document_by_id(client: Client, collection_path: str, document_id: str) -> Optional[DocumentSnapshot]:
    if not document_id.strip():
        return None

    snapshot = client.collection(collection_path).document(document_id.strip()).get()
    if not snapshot.exists:
        return None
    return snapshot


def run_collection_query(
    client: Client,
    collection_path: str,
    where_field: str = "",
    where_op: str = "==",
    where_value_raw: str = "",
    document_id: str = "",
    order_by: str = "",
    direction: str = "ASCENDING",
    limit: int = 50,
    apply_limit: bool = True,
    offset: int = 0,
) -> List[DocumentSnapshot]:
    if document_id.strip():
        snapshot = get_document_by_id(client, collection_path, document_id)
        return [snapshot] if snapshot else []

    query = client.collection(collection_path)

    if where_field and where_value_raw:
        where_value = parse_scalar(where_value_raw)
        query = query.where(filter=FieldFilter(where_field, where_op, where_value))

    if order_by:
        query = query.order_by(
            order_by,
            direction=(
                firestore.Query.DESCENDING
                if direction == "DESCENDING"
                else firestore.Query.ASCENDING
            ),
        )

    if offset > 0:
        query = query.offset(offset)

    if apply_limit:
        safe_limit = max(1, min(limit, 200))
        query = query.limit(safe_limit)

    return list(query.stream())


def parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if value == "":
        return ""

    lowered = value.lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def snapshot_to_editor_json(snapshot: DocumentSnapshot) -> str:
    payload = encode_firestore_value(snapshot.to_dict() or {})
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def encode_firestore_value(value: Any) -> Any:
    if isinstance(value, datetime):
        dt = value.astimezone(timezone.utc)
        return {"__type__": "timestamp", "value": dt.isoformat().replace("+00:00", "Z")}
    if hasattr(value, "path") and value.__class__.__name__ == "DocumentReference":
        return {"__type__": "reference", "path": value.path}
    if isinstance(value, GeoPoint):
        return {
            "__type__": "geopoint",
            "latitude": value.latitude,
            "longitude": value.longitude,
        }
    if isinstance(value, bytes):
        return {"__type__": "bytes", "base64": b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: encode_firestore_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [encode_firestore_value(item) for item in value]
    return value


def decode_editor_json(client: Client, raw_json: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise FirestoreBrowserError(f"Invalid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise FirestoreBrowserError("Document JSON must be an object at the top level.")

    return decode_firestore_value(client, parsed)


def decode_firestore_value(client: Client, value: Any) -> Any:
    if isinstance(value, dict):
        value_type = value.get("__type__")
        if value_type == "timestamp":
            raw = value.get("value", "")
            if not raw:
                raise FirestoreBrowserError("Timestamp values require a non-empty 'value'.")
            normalized = raw.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized)
        if value_type == "reference":
            path = (value.get("path") or "").strip()
            if not path:
                raise FirestoreBrowserError("Reference values require a non-empty 'path'.")
            return client.document(path)
        if value_type == "geopoint":
            return GeoPoint(value["latitude"], value["longitude"])
        if value_type == "bytes":
            encoded = value.get("base64", "")
            return b64decode(encoded.encode("ascii"))
        return {key: decode_firestore_value(client, item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_firestore_value(client, item) for item in value]
    return value


def flatten_snapshots(snapshots: Iterable[DocumentSnapshot]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for snapshot in snapshots:
        data = snapshot.to_dict() or {}
        preview_pairs = []
        for key, item in list(data.items())[:4]:
            preview_pairs.append(f"{key}={preview_scalar(item)}")
        rows.append(
            {
                "id": snapshot.id,
                "path": snapshot.reference.path,
                "create_time": snapshot.create_time,
                "update_time": snapshot.update_time,
                "field_count": len(data),
                "preview": ", ".join(preview_pairs),
                "all_fields": list(data.keys()),
                "table_fields": {
                    key: preview_scalar(item) for key, item in data.items() if not isinstance(item, (dict, list))
                },
            }
        )
    return rows


def preview_scalar(value: Any) -> str:
    if isinstance(value, dict):
        return "{...}"
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value)
    return text if len(text) <= 40 else f"{text[:37]}..."
