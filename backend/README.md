
# VitalTime Backend

The backend is a FastAPI coordination service for the VitalTime simulated
facility. It owns routing state, staff lifecycle simulation, dashboard
snapshots, and the SSE stream consumed by the web dashboard and iOS client.

## Install and run

```bash
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/dashboard` for the operator dashboard. The
health check is `/health`; the API documentation is available at `/docs`.

## Endpoints

- `POST /route-event` submits a room event and returns its routing decision.
- `GET /dashboard-state` returns the current staff and event snapshot.
- `GET /dashboard-stream` sends the same snapshots over SSE.
- `POST /clear-assignment` resolves an assigned event and retries pending work.
- `POST /reset-simulation` resets the in-memory demo scenario.

Runtime settings include `VITALTIME_TIME_SCALE`. Set `VITALTIME_SEED` to a
numeric value when repeatable seeded staff data is needed for tests or demos;
without it, live resets use fresh randomized fatigue inputs.
