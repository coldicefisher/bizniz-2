# FastAPI Skeleton

A production-ready FastAPI backend skeleton with JWT authentication, role-based access control, email verification, password reset, and Google OAuth. Ships with PostgreSQL via Docker Compose.

## Quick Start

```bash
cp .env.example .env
docker-compose up -d
```

- API: http://localhost:8000
- Swagger Docs: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- PostgreSQL: localhost:5432

### Local Development (without Docker)

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
# Edit .env with your database URL
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Features

### Authentication System

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/auth/register` | POST | Create account, assign "user" role, send verification email |
| `/api/v1/auth/login` | POST | Email + password login, returns access + refresh tokens |
| `/api/v1/auth/refresh` | POST | Exchange refresh token for new token pair |
| `/api/v1/auth/me` | GET | Get current user profile + roles (requires Bearer token) |
| `/api/v1/auth/verify-email` | POST | Verify email with token |
| `/api/v1/auth/resend-verification` | POST | Resend verification email |
| `/api/v1/auth/forgot-password` | POST | Send password reset email |
| `/api/v1/auth/reset-password` | POST | Reset password with token |
| `/api/v1/auth/oauth/google` | POST | Google OAuth code exchange |

### JWT Token Types

| Type | Lifetime | Purpose |
|------|----------|---------|
| `access` | 30 minutes | API authorization (Bearer token) |
| `refresh` | 30 days | Obtain new access tokens |
| `email_verify` | 24 hours | Email confirmation links |
| `password_reset` | 1 hour | Password reset links |

Each token type includes a `type` claim to prevent cross-use attacks.

### Role-Based Access Control

Users can hold multiple roles (e.g., "user", "admin"). Protect routes with the `require_roles` dependency:

```python
from app.core.auth import require_roles

@router.get("/admin-only")
async def admin_endpoint(user = Depends(require_roles("admin"))):
    return {"message": "Hello admin"}
```

### Email

Email is stubbed in development (logs to console). Set `MAIL_ENABLED=true` and implement a real provider (SendGrid, SES, SMTP) in `app/core/email.py` for production.

## Environment Variables

```bash
# Required
DATABASE_URL=postgresql+asyncpg://app:app_dev@db:5432/app
SECRET_KEY=change-me-in-production
ENVIRONMENT=development

# App
APP_NAME=App API
APP_BASE_URL=http://localhost

# Email (optional)
MAIL_ENABLED=false
MAIL_FROM=noreply@app.local

# OAuth (optional)
OAUTH_GOOGLE_CLIENT_ID=
OAUTH_GOOGLE_CLIENT_SECRET=
```

## Testing

Tests use an in-memory SQLite database for speed and isolation.

```bash
# All tests
python3 -m pytest -v

# By category
python3 -m pytest -m unit -v        # Unit tests (auth functions, schema validation)
python3 -m pytest -m functional -v  # API endpoint tests
python3 -m pytest -m e2e -v         # Full flow tests
```

**66 tests** across three tiers:

| Category | Tests | Scope |
|----------|-------|-------|
| Unit | 36 | Password hashing, token creation/validation, schema validation |
| Functional | 27 | Register, login, refresh, /me, email verification, password reset, role checks |
| E2E | 3 | Full registration-to-login flow, password reset flow, inactive user handling |

## Project Structure

```
app/
  main.py              # FastAPI app, CORS, route registration
  api/routes/auth.py   # Auth endpoints
  core/
    auth.py            # JWT, password hashing, get_current_user, require_roles
    config.py          # Pydantic settings (loads from .env)
    email.py           # Email stub
  db/
    base.py            # SQLAlchemy declarative base
    session.py         # Async session factory
  models/models.py     # User, Role, UserRole tables
  schemas/schemas.py   # Request/response Pydantic models
tests/
  conftest.py          # Fixtures: db_session, client, test_user, admin_user, auth_headers
  unit/                # Pure function tests
  functional/          # HTTP endpoint tests
  e2e/                 # Multi-step flow tests
```

## Tech Stack

- **Python 3.12** with async/await throughout
- **FastAPI 0.115** with automatic OpenAPI docs
- **SQLAlchemy 2.0** async ORM with asyncpg driver
- **Pydantic v2** for request/response validation
- **python-jose** for JWT token management
- **passlib + bcrypt** for password hashing
- **httpx** for async HTTP (OAuth code exchange)
- **PostgreSQL 16** via Docker Compose
- **pytest + pytest-asyncio** with aiosqlite test DB

## Docker

```yaml
# docker-compose.yml ships two services:
db:   postgres:16 (port 5432, persistent volume)
api:  Python 3.12 slim (port 8000, uvicorn)
```

```bash
docker-compose up -d          # Start services
docker-compose down           # Stop services
docker-compose down -v        # Stop and remove data volume
```

## Adding New Routes

1. Create a new file in `app/api/routes/`
2. Define a router: `router = APIRouter(prefix="/items", tags=["items"])`
3. Add your endpoints, using `Depends(get_current_user)` or `Depends(require_roles("admin"))` as needed
4. Register it in `app/main.py`: `app.include_router(router, prefix="/api/v1")`

## Adding New Models

1. Define your SQLAlchemy model in `app/models/models.py`
2. Create Pydantic schemas in `app/schemas/schemas.py`
3. Run migrations (add Alembic if not already configured)
