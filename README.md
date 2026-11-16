# VitalTime

VitalTime is a coordination layer for simulated care-facility events. It routes the right available staff member to an event; it is not a diagnostic system and uses no real patient data.

## Routing model

- **Tier 1:** emergency response. RN treatment qualification, shortest feasible ETA, then fatigue as a tie-breaker. A window-infeasible RN is used as the final safety fallback.
- **Tier 2:** validation and assessment. CNA/LPN candidates are preferred first using a 4:1 ETA-to-fatigue score; an RN is used only when no strict candidate can take the event.
- **Tier 3:** routine work. CNA/LPN candidates are preferred first using a 1:4 ETA-to-fatigue score; an RN is the last resort.

The backend is the source of truth. FastAPI exposes the routing API, dashboard state, and SSE stream; the static dashboard and the separate iOS client consume the same snapshot contract.

## Comparing against a call-light system

The dashboard's **VitalTime vs. call-light** panel runs a paired Monte Carlo
comparison through `POST /comparison/batch`. It runs the same generated event
stream through three isolated policies:

1. **VitalTime** uses the existing tier, qualification, ETA, fatigue, and
   assignment policy. The comparison does not modify the live routing store.
2. **Call-light A (typical)** models a notice delay, distance and busy-staff
   delay, role-blind first response, emergency recognition and RN call-up
   delay, and occasional duplicate responders.
3. **Call-light B (best case)** uses the nearest available responder with no
   notice delay. It remains role-blind except when the configured
   `degenerate_call_light` switch is enabled, which is the fairness control.

The call-light model is a baseline model, not measured call-light data. Its
assumptions are returned in every response and can be overridden in the
request. The default assumptions are:

| Parameter | Default | Meaning |
| --- | ---: | --- |
| `notice_delay_mean_minutes` | 0.75 | Mean time until a responder notices a call |
| `notice_delay_jitter_minutes` | 0.50 | Standard deviation of notice delay |
| `convergence_probability_tier2` | 0.35 | Chance of a duplicate validation responder |
| `convergence_probability_tier3` | 0.10 | Reserved for routine convergence modeling |
| `recognition_delay_minutes` | 1.00 | Time for a non-RN to recognize an emergency |
| `rn_call_delay_minutes` | 0.50 | Time to call an RN after recognition |
| `rn_answers_routine_probability` | 0.15 | Assumption retained for future routine-call behavior |
| `busy_renotice_delay_minutes` | 2.00 | Extra delay when all responders are busy |

### Reproducible experiment

```bash
curl -s http://127.0.0.1:8000/comparison/batch \
  -H 'content-type: application/json' \
  -d '{"trials": 200, "seed": 20261116, "events_per_day": 300}'
```

The same `seed` produces byte-identical JSON. Each trial generates 300 events
using a fixed 5% / 35% / 60% Tier 1/2/3 mix, rooms 101–112, and weighted
time bands with peaks at 6–9am, 11am–1pm, and 5–8pm. The generated events are
created once per trial and passed unchanged to every policy. Initial rosters
are also recreated separately for each policy, preventing fatigue or task
history from leaking between policies.

### Routing and fatigue equations

The live VitalTime policy uses the same constants in
`backend/app/routing/constants.py`:

- `ETA_SCALE = 3.0`, `FATIGUE_SCALE = 51.0`, `EPSILON = 0.15` minutes.
- Tier 1 chooses the lowest feasible ETA; candidates within `EPSILON` are
  broken by lowest fatigue.
- Tier 2 score is `4 * (ETA / 3) + 1 * (fatigue / 51)`.
- Tier 3 score is `1 * (ETA / 3) + 4 * (fatigue / 51)`.
- Qualification is a hard filter: Tier 1 requires RN treatment
  qualification; Tier 2/3 use assessment-capable CNA/LPN staff first, with
  RN as a last resort.
- `ETA` is the shortest-path travel time from the staff member's current
  room in the facility graph.
- `elapsed_fatigue(h) = h * (h + 1) / 2`, where `h` is hours in shift.
- `workload_fatigue = sum(3.0 * exp(-0.7 * task_age_hours))` for task history
  within the four-hour history window.
- `fatigue = elapsed_fatigue + workload_fatigue`.

The comparison uses those same ETA and fatigue functions as a common ruler.
Fatigue comparisons are therefore consistent, but they are not independent
validation of the VitalTime fatigue model.

### Reported metrics

Metrics are calculated per tier and overall. Mean values and 95% confidence
intervals use the per-trial means, with `CI95 = 1.96 * sample_stddev /
sqrt(number_of_trials)`. Response-time median and p95 are calculated over all
events in the policy run.

- **Response time:** event creation to first responder arrival.
- **SLA compliance:** response time within 2/5/10 minutes for Tier 1/2/3.
  Tier 1 uses time to the first RN, while first-arrival time remains separate.
- **Time to qualified responder:** event creation to first RN for Tier 1.
- **Qualification mismatch:** non-RN first handling of Tier 1, or RN handling
  of Tier 3.
- **Duplicate responders:** extra responders beyond the first; wasted minutes
  estimate duplicate travel and notice time.
- **Fatigue per event:** shared VitalTime fatigue formula at assignment.
- **Load distribution:** population standard deviation of task counts and
  fatigue across staff, plus maximum task count.

Results also include conservation and pairing flags. They are model outputs
about response, qualification fit, duplicate effort, and fatigue only; they
do not support claims about falls, mortality, clinical outcomes, or regulatory
performance.

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

This is a Layer 1 coordination MVP. Staffing, event volume, tier mix, and
call-light behavior are simulation assumptions, not clinical or operational
forecasts. The comparison is batch-only; it does not yet maintain live
comparison tallies for the SSE stream. The service stores live state in memory
and resets on restart.
