
import os, time
from typing import Optional
from datetime import timedelta
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from .common import *

os.environ.setdefault("SERVICE_NAME", "billing-service")
app = FastAPI(title="Billing Service", version="1.0.0")
app.middleware("http")(add_correlation_and_logging)
conn = db()
TAX_RATE = float(os.getenv("TAX_RATE", "0.05"))
CONSULTATION_FEE = float(os.getenv("CONSULTATION_FEE", "500"))
MEDICATION_FEE = float(os.getenv("MEDICATION_FEE", "200"))

class GenerateBill(BaseModel):
    appointment_id: int
    patient_id: int

class CancellationAdjustment(BaseModel):
    appointment_id: int
    slot_start: str

class UpdateStatus(BaseModel):
    status: str

@app.on_event("startup")
def startup():
    conn.execute('''CREATE TABLE IF NOT EXISTS bills(
        bill_id INTEGER PRIMARY KEY,
        patient_id INTEGER NOT NULL,
        appointment_id INTEGER NOT NULL UNIQUE,
        amount REAL NOT NULL,
        tax REAL DEFAULT 0,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        version INTEGER DEFAULT 1
    )''')
    load_csv_once(conn, "bills", "data/hms_bills_indian(1).csv", ["bill_id","patient_id","appointment_id","amount","status","created_at"])
    conn.execute("UPDATE bills SET tax=ROUND(amount * ?, 2) WHERE tax IS NULL OR tax=0", (TAX_RATE,))
    conn.commit()

@app.get("/health")
def health(): return {"status":"UP", "service":"billing-service"}
@app.get("/metrics")
def metrics(): return metrics_response()

@app.get("/v1/bills")
def list_bills(status: Optional[str]=None, patient_id: Optional[int]=None, limit:int=20, offset:int=0):
    limit, offset = paginate(limit, offset)
    where="WHERE 1=1"; params=[]
    if status: where += " AND status=?"; params.append(status.upper())
    if patient_id: where += " AND patient_id=?"; params.append(patient_id)
    rows = conn.execute(f"SELECT * FROM bills {where} ORDER BY bill_id LIMIT ? OFFSET ?", params+[limit,offset]).fetchall()
    return {"items": rows_to_dicts(rows), "limit": limit, "offset": offset}

@app.get("/v1/bills/{bill_id}")
def get_bill(bill_id: int):
    row = conn.execute("SELECT * FROM bills WHERE bill_id=?", (bill_id,)).fetchone()
    if not row: raise HTTPException(404, "Bill not found")
    return row_to_dict(row)

@app.get("/v1/bills/by-appointment/{appointment_id}")
def get_bill_by_appointment(appointment_id: int):
    row = conn.execute("SELECT * FROM bills WHERE appointment_id=?", (appointment_id,)).fetchone()
    if not row: raise HTTPException(404, "Bill not found")
    return row_to_dict(row)

@app.post("/v1/bills/generate", status_code=201)
def generate_bill(payload: GenerateBill, x_role: Optional[str]=Header("admin")):
    start = time.time()
    base = CONSULTATION_FEE + MEDICATION_FEE
    tax = round(base * TAX_RATE, 2)
    amount = round(base + tax, 2)
    with conn:
        existing = conn.execute("SELECT bill_id FROM bills WHERE appointment_id=?", (payload.appointment_id,)).fetchone()
        if existing:
            return get_bill(existing["bill_id"])
        cur = conn.execute("INSERT INTO bills(patient_id,appointment_id,amount,tax,status,created_at,version) VALUES(?,?,?,?, 'OPEN', ?,1)",
                           (payload.patient_id,payload.appointment_id,amount,tax,now_utc()))
    bill_creation_latency_ms.observe((time.time()-start)*1000)
    return get_bill(cur.lastrowid)

@app.post("/v1/bills/adjust-for-cancellation")
def adjust_for_cancellation(payload: CancellationAdjustment):
    row = conn.execute("SELECT * FROM bills WHERE appointment_id=?", (payload.appointment_id,)).fetchone()
    if not row:
        return {"message":"No bill found; nothing to adjust"}
    if row["status"] == "PAID":
        status = "REFUND_PENDING"
    else:
        status = "VOID" if parse_dt(payload.slot_start) > datetime.now(timezone.utc)+timedelta(hours=2) else "PARTIAL_CHARGE"
    amount = 0 if status == "VOID" else round(float(row["amount"])*0.50, 2) if status == "PARTIAL_CHARGE" else row["amount"]
    conn.execute("UPDATE bills SET status=?, amount=?, version=version+1 WHERE appointment_id=?", (status, amount, payload.appointment_id))
    conn.commit()
    return get_bill_by_appointment(payload.appointment_id)

@app.patch("/v1/bills/{bill_id}/status")
def update_status(bill_id: int, payload: UpdateStatus):
    allowed = {"OPEN","PAID","VOID","PARTIAL_CHARGE","REFUND_PENDING","REFUNDED"}
    if payload.status.upper() not in allowed: raise HTTPException(400, "Invalid bill status")
    conn.execute("UPDATE bills SET status=?, version=version+1 WHERE bill_id=?", (payload.status.upper(), bill_id))
    conn.commit()
    return get_bill(bill_id)
