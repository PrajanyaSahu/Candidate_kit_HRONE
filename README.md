# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run

### 1. Start MongoDB (Docker Engine)
MongoDB 6.0+ is required (the analytics use `$densify`, `$setWindowFields` and `$lookup` with a sub-pipeline). You can start a local instance using Docker:
```bash
docker run -d --name mongo -p 27017:27017 mongo:7.0
```

### 2. Start the API
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # or export MONGO_URI / MONGO_DB (real env vars win over .env)
uvicorn app.main:app --port 8000
```
Indexes are created automatically at startup; there are no manual steps.

## Status
Every endpoint in `openapi.yaml` is implemented: punch-in/out, PATCH regularization with history, the three
aggregation endpoints (department summary, late leaderboard, department trend), the employee monthly summary
and `/admin/explain/{endpoint}`. `REVIEW.md` and `DECISIONS.md` are filled in.

## Tests (optional, not needed to run the app)
```bash
pip install pytest httpx mongomock
pytest tests                                   # rules R1-R5, CRUD, status codes, races (no MongoDB server needed)
MONGO_DB=attendance_scratch python tests/live_smoke.py   # analytics + explain vs a Python reference; needs a real MongoDB
```
`live_smoke.py` writes to (and then drops) the database named in `MONGO_DB`: use a scratch one.

## Notes
- Attendance dates, shifts and the weekday logic are IST; instants are epoch milliseconds in the API and BSON dates in MongoDB.
- `PATCH` uses optimistic locking: if the record changed between read and write, the loser gets 409 and no history entry is lost.
