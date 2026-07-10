# backend/app/main.py
#
# CHANGE: legacy routers (auth, users, vitals, alerts, triage, audit) and
# storage.py are stripped. They were pre-existing scaffold code unrelated
# to VitalTime's routing engine, and they were the source of the bcrypt
# 5.0.0 / passlib startup crash that took the whole app down on Render
# (see session log). Removing them shrinks the failure surface, drops
# passlib/jose/email-validator/etc. from the dependency tree, and speeds
# the cold-start.
#
# If you ever want to re-add any of these (e.g. a real auth layer), do
# it as a separate deployable service or a router that's guarded from
# blocking startup on import-time crashes.
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import dashboard, routing

app = FastAPI(title="VitalTime API", version="0.5.0")
app.include_router(dashboard.router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(routing.router)


@app.get("/health", tags=["health"])
def health_check():
    return {"status": "ok"}
