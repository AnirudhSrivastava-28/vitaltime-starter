# backend/app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import auth, users, vitals, alerts, triage, audit, routing

app = FastAPI(title="VitalTime API", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(vitals.router, prefix="/vitals", tags=["vitals"])
app.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
app.include_router(triage.router, prefix="/triage", tags=["triage"])
app.include_router(audit.router, prefix="/audit", tags=["audit"])
app.include_router(routing.router)

@app.get("/health", tags=["health"])
def health_check():
    return {"status": "ok"}
