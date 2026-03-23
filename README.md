# Firestore Browser

A local single-user Firestore browser/editor built with Python and FastAPI.

## Features

- Connect with a Firebase Admin SDK service account JSON file picker
- Browse root collections and subcollections
- Query a collection with simple filters, ordering, and limit
- View and edit documents as JSON
- Create and delete documents
- Optional read-only mode for safer inspection

## Quick start

1. Create a virtual environment:

```bash
python3 -m venv .venv
```

2. Install dependencies with the virtual environment:

```bash
.venv/bin/pip install -r requirements.txt
```

3. Start the app:

```bash
.venv/bin/uvicorn app.main:app --reload
```

4. Open <http://127.0.0.1:8000>
5. Use the file picker in the UI to choose your Firebase Admin SDK JSON key

## Environment variables

- `FIREBASE_CREDENTIALS`: Optional absolute path to preload your Firebase Admin SDK JSON file
- `FIREBASE_PROJECT_ID`: Optional project override
- `APP_READ_ONLY`: Set to `true` to disable writes

## Editable JSON format

Simple Firestore values can be edited as regular JSON.

Special Firestore values use tagged objects:

```json
{
  "createdAt": { "__type__": "timestamp", "value": "2026-03-23T10:00:00Z" },
  "ownerRef": { "__type__": "reference", "path": "users/alice" },
  "location": { "__type__": "geopoint", "latitude": 25.033, "longitude": 121.5654 },
  "avatar": { "__type__": "bytes", "base64": "SGVsbG8=" }
}
```

## Notes

- This tool is intentionally local and does not include multi-user auth.
- Uploaded credentials are stored in `.local_data/` for local reuse.
- Delete operations require explicit confirmation.
- Firestore composite index requirements still apply to your queries.
