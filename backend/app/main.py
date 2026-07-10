# backend/app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import routing, dashboard

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
