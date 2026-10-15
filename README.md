# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne engineering assignment. The API contract and business rules are documented in [openapi.yaml](./openapi.yaml), and the MongoDB document structure is described in [DATA_MODEL.md](./DATA_MODEL.md).

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (MongoDB Atlas is supported)

## Local setup

From the project root, run the following commands in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"

uvicorn app.main:app --port 8000
```

For MongoDB Atlas, set `MONGO_URI` and `MONGO_DB` to your database connection settings before starting the application. The application also supports a local `.env` file for development, with environment variables taking precedence. Do not commit credentials or `.env` files.

The application creates the required indexes during startup. If existing data contains duplicate values that violate unique indexes, startup may fail. Resolve these conflicts deliberately rather than deleting or modifying records automatically.

## Sample data

The project includes `sample_seed.py` for loading sample data. Inspect the script before running it and use only a disposable development database, since seeding may modify existing data.

```powershell
python sample_seed.py
```

## Run and test

Start the application from the project root:

```powershell
uvicorn app.main:app --port 8000
```

Run the unit tests in a separate terminal:

```powershell
python -m unittest discover -s tests -v
```

Interactive API documentation is available at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## API overview

The implemented routes cover:

- Health checks
- Employee creation and listing
- Attendance punch-in, punch-out, listing, and regularization
- Employee monthly analytics
- Department attendance summaries
- Late-arrival leaderboard
- Department attendance trends
- MongoDB query explain information

Analytics are calculated using MongoDB aggregation pipelines.

Unit tests use mocks and fakes for some database operations. Passing the unit tests alone does not establish that every operation works against a live MongoDB database or that indexes are selected for every query and dataset.

## Additional documentation

- [REVIEW.md](./REVIEW.md): Findings from reviewing the starter code and the fixes made.
- [DECISIONS.md](./DECISIONS.md): Implementation decisions and answers to the assignment questions.
- [openapi.yaml](./openapi.yaml): API contract.
- [DATA_MODEL.md](./DATA_MODEL.md): MongoDB collections and document structure.