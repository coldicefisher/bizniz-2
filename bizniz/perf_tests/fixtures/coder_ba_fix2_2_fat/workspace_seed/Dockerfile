FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

# Dev mode: --reload watches the bind-mounted /app for source changes
# and re-runs the FastAPI lifespan (Base.metadata.create_all) on each
# reload. Without this, models added in later milestones (e.g. M2
# Contact, M3 Company) never get their tables created — the running
# uvicorn instance keeps using the Base.metadata snapshot from initial
# startup, smoke phase then 500s on "relation 'contacts' does not
# exist" (CRM v1, 2026-05-15). Production builds replace this CMD
# with a no-reload variant + Alembic migrations.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/app"]
