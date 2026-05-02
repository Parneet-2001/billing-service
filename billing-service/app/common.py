
import csv, json, logging, os, re, sqlite3, time, uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/service.db")

class JsonFormatter(logging.Formatter):
    def format(self, record):
        msg = record.getMessage()
        msg = mask_pii(msg)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": os.getenv("SERVICE_NAME", "unknown-service"),
            "message": msg,
        }
        if hasattr(record, "correlation_id"):
            payload["correlationId"] = record.correlation_id
        return json.dumps(payload)

def setup_logger():
    logger = logging.getLogger(os.getenv("SERVICE_NAME", "hms"))
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger

logger = setup_logger()

EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+)")
PHONE_RE = re.compile(r"\b(\d{2})\d{6}(\d{2})\b")

def mask_pii(value: str) -> str:
    value = EMAIL_RE.sub(r"\1***\2", str(value))
    value = PHONE_RE.sub(r"\1******\2", value)
    return value

def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def parse_dt(value: str):
    if not value:
        raise ValueError("timestamp is required")
    v = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        # Provided seed files use 'YYYY-MM-DD HH:MM:SS.microsecond'
        dt = datetime.fromisoformat(v.replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def db():
    Path = os.path
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def row_to_dict(row):
    return dict(row) if row else None

def rows_to_dicts(rows):
    return [dict(r) for r in rows]

def load_csv_once(conn, table, csv_path, columns):
    count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    if count > 0 or not os.path.exists(csv_path):
        return
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        placeholders = ",".join(["?"] * len(columns))
        colsql = ",".join(columns)
        for row in reader:
            vals = [row.get(c, "") for c in columns]
            conn.execute(f"INSERT OR IGNORE INTO {table} ({colsql}) VALUES ({placeholders})", vals)
    conn.commit()
    logger.info(f"Seeded {table} from {csv_path}")

def error(code, message, correlation_id, status=400):
    return JSONResponse(status_code=status, content={"code": code, "message": message, "correlationId": correlation_id})

def require_role(x_role: Optional[str], allowed):
    if not x_role:
        raise HTTPException(status_code=401, detail="X-Role header required")
    role = x_role.lower()
    if role not in allowed:
        raise HTTPException(status_code=403, detail=f"Role '{role}' not allowed. Allowed: {sorted(allowed)}")

def paginate(limit: int, offset: int):
    limit = max(1, min(limit or 20, 100))
    offset = max(0, offset or 0)
    return limit, offset

REQUEST_COUNT = Counter("http_requests_total", "Total HTTP requests", ["service", "path", "method", "status"])
REQUEST_LATENCY = Histogram("http_request_latency_ms", "Request latency in ms", ["service", "path", "method"])
appointments_created_total = Counter("appointments_created_total", "Appointments created")
bill_creation_latency_ms = Histogram("bill_creation_latency_ms", "Bill creation latency in ms")
payments_failed_total = Counter("payments_failed_total", "Failed payments")

async def add_correlation_and_logging(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-Id", str(uuid.uuid4()))
    start = time.time()
    try:
        response = await call_next(request)
    except HTTPException as e:
        return error("HTTP_ERROR", str(e.detail), correlation_id, e.status_code)
    except Exception as e:
        logger.exception("Unhandled error", extra={"correlation_id": correlation_id})
        return error("INTERNAL_ERROR", "Unexpected server error", correlation_id, 500)
    elapsed_ms = (time.time() - start) * 1000
    REQUEST_COUNT.labels(os.getenv("SERVICE_NAME", "unknown"), request.url.path, request.method, response.status_code).inc()
    REQUEST_LATENCY.labels(os.getenv("SERVICE_NAME", "unknown"), request.url.path, request.method).observe(elapsed_ms)
    response.headers["X-Correlation-Id"] = correlation_id
    logger.info(f"{request.method} {request.url.path} status={response.status_code} latencyMs={elapsed_ms:.2f}", extra={"correlation_id": correlation_id})
    return response

def metrics_response():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
