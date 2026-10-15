# Employee Attendance & Analytics API

FastAPI + PyMongo implementation of the API contract in `openapi.yaml`. It supports employee creation/listing, attendance punch-in/out and regularization, monthly and department analytics, a late leaderboard, daily trends, and MongoDB explain output. Design notes and the starter-code review are in `DECISIONS.md` and `REVIEW.md`.

## Run locally

Requires Python 3.11+, MongoDB 6.0+, and `MONGO_URI` / `MONGO_DB` (environment variables or a local `.env` file). From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000
```

The API creates its indexes at startup. Do not commit `.env` files or credentials.
