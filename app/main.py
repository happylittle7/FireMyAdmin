from __future__ import annotations

from collections import deque
from dataclasses import asdict
import hashlib
from pathlib import Path
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .firestore_service import (
    ConnectionConfig,
    FirestoreBrowserError,
    decode_editor_json,
    flatten_snapshots,
    get_client,
    get_document,
    list_root_collections,
    list_subcollections,
    run_collection_query,
    snapshot_to_editor_json,
)


BASE_DIR = Path(__file__).resolve().parent
LOCAL_DATA_DIR = BASE_DIR.parent / ".local_data"
LOCAL_DATA_DIR.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="FireMyAdmin")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

runtime_connection: Dict[str, str] = {
    "credentials_path": settings.firebase_credentials,
    "project_id": settings.firebase_project_id,
}
recent_views: deque[Dict[str, str]] = deque(maxlen=10)
collection_cache: Dict[str, Any] = {"signature": "", "collections": [], "loaded_at": 0.0}


def build_collection_breadcrumbs(collection_path: str) -> list[Dict[str, str]]:
    if not collection_path:
        return []

    parts = collection_path.split("/")
    crumbs = [{"label": "Collections", "href": "/"}]
    for index in range(0, len(parts), 2):
        current_path = "/".join(parts[: index + 1])
        crumbs.append({"label": parts[index], "href": f"/collections/{current_path}"})
        if index + 1 < len(parts):
            doc_path = "/".join(parts[: index + 2])
            crumbs.append({"label": parts[index + 1], "href": f"/documents/{doc_path}"})
    return crumbs


def build_document_breadcrumbs(document_path: str) -> list[Dict[str, str]]:
    if not document_path:
        return []

    parts = document_path.split("/")
    crumbs = [{"label": "Collections", "href": "/"}]
    for index in range(len(parts)):
        current_path = "/".join(parts[: index + 1])
        if index % 2 == 0:
            crumbs.append({"label": parts[index], "href": f"/collections/{current_path}"})
        else:
            crumbs.append({"label": parts[index], "href": f"/documents/{current_path}"})
    return crumbs


def build_parent_link(collection_path: str = "", document_path: str = "") -> Dict[str, str]:
    if document_path:
        parent_collection = document_path.rsplit("/", 1)[0]
        return {"label": "上一層 collection", "href": f"/collections/{parent_collection}"}

    if collection_path:
        parts = collection_path.split("/")
        if len(parts) == 1:
            return {"label": "回到首頁", "href": "/"}
        parent_document = "/".join(parts[:-1])
        return {"label": "上一層 document", "href": f"/documents/{parent_document}"}

    return {}


def build_table_columns(
    documents: list[Dict[str, Any]],
    sort_field: str = "",
    sort_direction: str = "ASCENDING",
) -> list[Dict[str, Any]]:
    frequencies: Dict[str, int] = {}
    for item in documents:
        for key in item.get("table_fields", {}):
            frequencies[key] = frequencies.get(key, 0) + 1

    ordered_fields = sorted(frequencies, key=lambda key: (-frequencies[key], key))
    if sort_field and sort_field not in {"__name__", ""} and sort_field not in ordered_fields:
        ordered_fields.insert(0, sort_field)

    columns: list[Dict[str, Any]] = [{"label": "Document ID", "field": "__name__", "sortable": True}]
    for field in ordered_fields[:4]:
        columns.append({"label": field, "field": field, "sortable": True})
    columns.extend(
        [
            {"label": "Fields", "field": "", "sortable": False},
            {"label": "Updated", "field": "", "sortable": False},
        ]
    )
    for column in columns:
        is_active = bool(column["field"]) and column["field"] == sort_field
        column["active"] = is_active
        column["next_direction"] = next_sort_direction(sort_field, sort_direction, column["field"]) if column["sortable"] else ""
    return columns


def build_query_fields(documents: list[Dict[str, Any]], current_field: str = "") -> list[str]:
    frequencies: Dict[str, int] = {}
    for item in documents:
        for key in item.get("all_fields", item.get("table_fields", {})):
            frequencies[key] = frequencies.get(key, 0) + 1

    fields = sorted(frequencies, key=lambda key: (-frequencies[key], key))
    if current_field and current_field not in fields:
        fields.insert(0, current_field)
    return fields


def next_sort_direction(current_order_by: str, current_direction: str, clicked_field: str) -> str:
    if current_order_by == clicked_field and current_direction == "ASCENDING":
        return "DESCENDING"
    return "ASCENDING"


def build_pagination_window(current_page: int, has_next: bool, window: int = 2) -> list[int]:
    start_page = max(1, current_page - window)
    end_page = current_page + window + (1 if has_next else 0)
    return list(range(start_page, max(start_page, end_page) + 1))


def remember_recent(kind: str, path: str) -> None:
    href = f"/collections/{path}" if kind == "collection" else f"/documents/{path}"
    label = path.split("/")[-1] if path else "Home"
    entry = {"kind": kind, "path": path, "href": href, "label": label}
    deduped = [item for item in recent_views if item["href"] != href]
    recent_views.clear()
    recent_views.extend(deduped)
    recent_views.appendleft(entry)


def connection_signature(config: ConnectionConfig) -> str:
    return f"{config.credentials_path}::{config.project_id}"


def get_cached_root_collections(config: ConnectionConfig) -> list[str]:
    signature = connection_signature(config)
    now = time.time()
    if (
        collection_cache["signature"] == signature
        and collection_cache["collections"]
        and now - collection_cache["loaded_at"] < 15
    ):
        return collection_cache["collections"]

    client = get_client(config)
    collections = list_root_collections(client)
    collection_cache["signature"] = signature
    collection_cache["collections"] = collections
    collection_cache["loaded_at"] = now
    return collections


def build_sidebar_tree(
    root_collections: list[str],
    current_collection: str = "",
    current_document_path: str = "",
    documents: Optional[list[Dict[str, Any]]] = None,
    subcollections: Optional[list[str]] = None,
) -> list[Dict[str, Any]]:
    active_collection_path = current_collection
    active_document_path = current_document_path
    active_path = active_document_path or active_collection_path
    active_parts = active_path.split("/") if active_path else []

    def make_collection_node(label: str, path: str, active: bool = False, expanded: bool = False) -> Dict[str, Any]:
        return {
            "label": label,
            "title": path,
            "href": f"/collections/{path}",
            "kind": "collection",
            "active": active,
            "expanded": expanded,
            "children": [],
        }

    def make_document_node(label: str, path: str, meta: str = "", active: bool = False, expanded: bool = False) -> Dict[str, Any]:
        return {
            "label": label,
            "title": path,
            "href": f"/documents/{path}",
            "kind": "document",
            "active": active,
            "expanded": expanded,
            "meta": meta,
            "children": [],
        }

    tree = [
        make_collection_node(
            collection,
            collection,
            active=(active_collection_path == collection),
            expanded=(bool(active_parts) and active_parts[0] == collection),
        )
        for collection in root_collections
    ]

    if not active_parts:
        return tree

    current_nodes = tree
    index = 0
    while index < len(active_parts):
        part = active_parts[index]
        is_collection = index % 2 == 0
        path = "/".join(active_parts[: index + 1])
        existing = next((node for node in current_nodes if node["label"] == part), None)

        if existing is None:
            existing = (
                make_collection_node(part, path, active=(active_collection_path == path), expanded=True)
                if is_collection
                else make_document_node(part, path, active=(active_document_path == path), expanded=True)
            )
            current_nodes.append(existing)

        existing["expanded"] = True
        existing["active"] = (active_collection_path == path) if is_collection else (active_document_path == path)
        existing["title"] = path

        if index == len(active_parts) - 1:
            if is_collection:
                for item in documents or []:
                    existing["children"].append(
                        make_document_node(
                            item["id"],
                            item["path"],
                            meta=f"{item['field_count']} fields",
                            active=(item["path"] == active_document_path),
                        )
                    )
            else:
                for collection in subcollections or []:
                    child_collection_path = f"{path}/{collection}"
                    existing["children"].append(
                        make_collection_node(
                            collection,
                            child_collection_path,
                            active=(active_collection_path == child_collection_path),
                        )
                    )

        current_nodes = existing["children"]
        index += 1

    return tree


def get_connection_config() -> ConnectionConfig:
    return ConnectionConfig(
        credentials_path=runtime_connection.get("credentials_path", "").strip(),
        project_id=runtime_connection.get("project_id", "").strip(),
    )


def get_base_context(request: Request, **extra: Any) -> Dict[str, Any]:
    connection = get_connection_config()
    context = {
        "request": request,
        "settings": settings,
        "connection": asdict(connection),
        "collections": [],
        "current_collection": "",
        "current_document_path": "",
        "error": "",
        "message": "",
        "sidebar_tree": [],
        "recent_views": list(recent_views),
        "parent_link": {},
    }
    context.update(extra)

    if connection.credentials_path:
        try:
            context["collections"] = get_cached_root_collections(connection)
            context["sidebar_tree"] = build_sidebar_tree(context["collections"])
        except Exception as exc:  # pragma: no cover - UI fallback
            context["error"] = str(exc)
    return context


def persist_uploaded_credentials(upload: UploadFile) -> str:
    filename = upload.filename or "service-account.json"
    suffix = Path(filename).suffix or ".json"
    raw = upload.file.read()
    if not raw:
        raise ValueError("Uploaded credentials file is empty.")

    digest = hashlib.sha256(raw).hexdigest()[:12]
    target = LOCAL_DATA_DIR / f"firebase-key-{digest}{suffix}"
    target.write_bytes(raw)
    return str(target)


@app.get("/")
async def home(request: Request, message: str = ""):
    context = get_base_context(request, message=message)
    return templates.TemplateResponse("index.html", context)


@app.post("/connect")
async def connect(
    credentials_file: Optional[UploadFile] = File(default=None),
    project_id: str = Form(""),
):
    if credentials_file and credentials_file.filename:
        runtime_connection["credentials_path"] = persist_uploaded_credentials(credentials_file)
    elif not runtime_connection.get("credentials_path", "").strip():
        return RedirectResponse("/?message=Please+choose+a+Service+Account+JSON+file", status_code=303)
    runtime_connection["project_id"] = project_id.strip()
    collection_cache["signature"] = ""
    collection_cache["collections"] = []
    collection_cache["loaded_at"] = 0.0
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout():
    runtime_connection["credentials_path"] = ""
    runtime_connection["project_id"] = ""
    collection_cache["signature"] = ""
    collection_cache["collections"] = []
    collection_cache["loaded_at"] = 0.0
    recent_views.clear()
    return RedirectResponse("/?message=Disconnected", status_code=303)


@app.get("/collections/{collection_path:path}")
async def view_collection(
    request: Request,
    collection_path: str,
    page: int = 1,
    view_mode: str = "table",
    document_id: str = "",
    where_field: str = "",
    where_op: str = "==",
    where_value: str = "",
    order_by: str = "",
    direction: str = "ASCENDING",
    limit: int = 20,
):
    context = get_base_context(
        request,
        current_collection=collection_path,
        view_mode="table" if view_mode == "table" else "grid",
        page=max(1, page),
        query={
            "page": max(1, page),
            "view_mode": view_mode,
            "document_id": document_id,
            "where_field": where_field,
            "where_op": where_op,
            "where_value": where_value,
            "order_by": order_by,
            "direction": direction,
            "limit": limit,
        },
        documents=[],
        breadcrumbs=build_collection_breadcrumbs(collection_path),
        pagination={
            "page": max(1, page),
            "has_prev": max(1, page) > 1,
            "has_next": False,
            "enabled": True,
            "pages": [max(1, page)],
        },
    )
    remember_recent("collection", collection_path)
    context["recent_views"] = list(recent_views)
    context["parent_link"] = build_parent_link(collection_path=collection_path)

    try:
        client = get_client(get_connection_config())
        has_document_id_query = bool(document_id.strip())
        invalid_sort_field = order_by in {"__updated__", "__field_count__"}
        if invalid_sort_field:
            context["message"] = "Updated 與 Fields 不能做 Firestore 全域排序，已忽略該排序條件。"
        firestore_order_by = "" if invalid_sort_field else order_by
        safe_page = max(1, page)
        safe_limit = max(1, min(limit, 200))
        page_fetch_limit = safe_limit + 1 if not has_document_id_query else safe_limit
        page_offset = (safe_page - 1) * safe_limit if not has_document_id_query else 0
        snapshots = run_collection_query(
            client=client,
            collection_path=collection_path,
            document_id=document_id.strip(),
            where_field=where_field,
            where_op=where_op,
            where_value_raw=where_value,
            order_by=firestore_order_by,
            direction=direction,
            limit=page_fetch_limit,
            apply_limit=True,
            offset=page_offset,
        )
        fetched_documents = flatten_snapshots(snapshots)
        if not has_document_id_query:
            has_next_page = len(fetched_documents) > safe_limit
            context["pagination"] = {
                "page": safe_page,
                "has_prev": safe_page > 1,
                "has_next": has_next_page,
                "enabled": True,
                "pages": build_pagination_window(safe_page, has_next_page),
            }
            context["documents"] = fetched_documents[:safe_limit]
        else:
            context["pagination"] = {
                "page": 1,
                "has_prev": False,
                "has_next": False,
                "enabled": True,
                "pages": [1],
            }
            context["documents"] = fetched_documents[:safe_limit]
        context["query_fields"] = build_query_fields(context["documents"], current_field=where_field)
        context["table_columns"] = build_table_columns(
            context["documents"],
            sort_field=order_by,
            sort_direction=direction,
        )
        context["sidebar_tree"] = build_sidebar_tree(
            context["collections"],
            current_collection=collection_path,
            documents=context["documents"],
        )
        context["view_mode"] = "table" if view_mode == "table" else "grid"
        context["current_order_by"] = order_by
        context["current_direction"] = direction
    except Exception as exc:
        context["error"] = str(exc)

    return templates.TemplateResponse("collection.html", context)


@app.get("/documents/{document_path:path}")
async def view_document(request: Request, document_path: str, message: str = ""):
    context = get_base_context(
        request,
        current_document_path=document_path,
        document_json="{}",
        subcollections=[],
        document_meta={},
        message=message,
        breadcrumbs=build_document_breadcrumbs(document_path),
    )
    remember_recent("document", document_path)
    context["recent_views"] = list(recent_views)
    context["parent_link"] = build_parent_link(document_path=document_path)

    try:
        client = get_client(get_connection_config())
        snapshot = get_document(client, document_path)
        context["document_json"] = snapshot_to_editor_json(snapshot)
        context["document_meta"] = {
            "id": snapshot.id,
            "path": snapshot.reference.path,
            "create_time": snapshot.create_time,
            "update_time": snapshot.update_time,
            "exists": snapshot.exists,
        }
        context["subcollections"] = list_subcollections(snapshot.reference)
        remember_recent("document", document_path)
        context["recent_views"] = list(recent_views)
        context["sidebar_tree"] = build_sidebar_tree(
            context["collections"],
            current_document_path=document_path,
            subcollections=context["subcollections"],
        )
    except Exception as exc:
        context["error"] = str(exc)

    return templates.TemplateResponse("document.html", context)


@app.post("/documents/{document_path:path}/save")
async def save_document(request: Request, document_path: str, document_json: str = Form(...)):
    if settings.read_only:
        return templates.TemplateResponse(
            "document.html",
            get_base_context(
                request,
                current_document_path=document_path,
                error="Read-only mode is enabled. Writes are disabled.",
                document_json=document_json,
                subcollections=[],
                document_meta={"path": document_path},
            ),
            status_code=400,
        )

    try:
        client = get_client(get_connection_config())
        payload = decode_editor_json(client, document_json)
        client.document(document_path).set(payload)
        return RedirectResponse(
            f"/documents/{document_path}?message=Document+saved",
            status_code=303,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "document.html",
            get_base_context(
                request,
                current_document_path=document_path,
                error=str(exc),
                document_json=document_json,
                subcollections=[],
                document_meta={"path": document_path},
            ),
            status_code=400,
        )


@app.post("/collections/{collection_path:path}/create")
async def create_document(
    request: Request,
    collection_path: str,
    document_id: str = Form(""),
    document_json: str = Form("{}"),
):
    if settings.read_only:
        return templates.TemplateResponse(
            "collection.html",
            get_base_context(
                request,
                current_collection=collection_path,
                error="Read-only mode is enabled. Writes are disabled.",
                documents=[],
                query={},
            ),
            status_code=400,
        )

    try:
        client = get_client(get_connection_config())
        payload = decode_editor_json(client, document_json)
        collection_ref = client.collection(collection_path)
        if document_id.strip():
            doc_ref = collection_ref.document(document_id.strip())
            doc_ref.set(payload)
        else:
            doc_ref = collection_ref.add(payload)[1]
        return RedirectResponse(f"/documents/{doc_ref.path}?message=Document+created", status_code=303)
    except Exception as exc:
        return templates.TemplateResponse(
            "collection.html",
            get_base_context(
                request,
                current_collection=collection_path,
                error=str(exc),
                documents=[],
                query={},
            ),
            status_code=400,
        )


@app.post("/documents/{document_path:path}/delete")
async def delete_document(
    request: Request,
    document_path: str,
    confirm_text: str = Form(""),
):
    if settings.read_only:
        return templates.TemplateResponse(
            "document.html",
            get_base_context(
                request,
                current_document_path=document_path,
                error="Read-only mode is enabled. Writes are disabled.",
                document_json="{}",
                subcollections=[],
                document_meta={"path": document_path},
            ),
            status_code=400,
        )

    if confirm_text.strip() != "DELETE":
        return templates.TemplateResponse(
            "document.html",
            get_base_context(
                request,
                current_document_path=document_path,
                error='Type "DELETE" to confirm deletion.',
                document_json="{}",
                subcollections=[],
                document_meta={"path": document_path},
            ),
            status_code=400,
        )

    try:
        client = get_client(get_connection_config())
        client.document(document_path).delete()
        parent_collection = document_path.rsplit("/", 1)[0]
        return RedirectResponse(
            f"/collections/{parent_collection}",
            status_code=303,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "document.html",
            get_base_context(
                request,
                current_document_path=document_path,
                error=str(exc),
                document_json="{}",
                subcollections=[],
                document_meta={"path": document_path},
            ),
            status_code=400,
        )
