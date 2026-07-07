
# VitalTime Drop‑ins (Backend)

## Install & Run
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
# then open http://127.0.0.1:8000/docs
```

## Quick Test
1) Get token (demo user: alice / secret)
- POST `/auth/token` with form fields `username=alice`, `password=secret`

2) Create a vital (will auto‑alert if out of range)
- POST `/vitals/` with JSON: `{"user_id":1,"type":"hr","value":150}` using Bearer token

3) List alerts
- GET `/alerts/` with the same token
```
