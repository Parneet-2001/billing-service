# billing-service

FastAPI microservice for the Hospital Management System.

## Run locally
```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8004 --reload
```

## Useful URLs
- Health: `GET /health`
- OpenAPI UI: `GET /docs`
- OpenAPI JSON: `GET /openapi.json`
- Metrics: `GET /metrics`

## RBAC
Protected write endpoints require `X-Role` header, e.g. `reception`, `doctor`, `billing`, or `admin`.
