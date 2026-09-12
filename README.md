# VitalTime

VitalTime is a coordination layer for simulated care-facility events. It routes the right available staff member to an event; it is not a diagnostic system and uses no real patient data.

## Routing model

- **Tier 1:** emergency response. RN treatment qualification, shortest feasible ETA, then fatigue as a tie-breaker. A window-infeasible RN is used as the final safety fallback.
- **Tier 2:** validation and assessment. CNA/LPN candidates are preferred first using a 4:1 ETA-to-fatigue score; an RN is used only when no strict candidate can take the event.
- **Tier 3:** routine work. CNA/LPN candidates are preferred first using a 1:4 ETA-to-fatigue score; an RN is the last resort.

The backend is the source of truth. FastAPI exposes the routing API, dashboard state, and SSE stream; the static dashboard and the separate iOS client consume the same snapshot contract.

## Run locally

```bash
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open `/dashboard`; `/health` is the deploy health check. Render configuration is in `render.yaml`. The iOS project is included separately under `VitalTime iOS/` and requires macOS/Xcode to build.

## Known limitations

This is a Layer 1 coordination MVP. Escalation from Tier 2 to Tier 1 and a native iOS facility map are not implemented. Staffing, event volume, and tier mix are simulation assumptions, not clinical or operational forecasts. The service stores state in memory and resets on restart.
