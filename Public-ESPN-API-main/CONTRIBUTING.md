# Contributing to Public-ESPN-API

Thank you for your interest in contributing! This project documents the unofficial ESPN API and provides a Django-based service for consuming it.

## Ways to Contribute

### 📖 Documentation
- Add or correct endpoint URLs
- Add missing league slugs for a sport
- Improve or add curl examples
- Add response schema examples to `docs/response_schemas.md`
- Fix errors, typos, or outdated information

### 🐛 Report a Bug
Open an issue using the **🐛 Bug Report** template. Please include:
- The endpoint URL you used
- What you expected
- What you actually received (status code, response snippet)

### 🆕 Report a Missing Endpoint
Open an issue using the **🔍 Missing Endpoint** template. If you've found an ESPN API endpoint not documented here, we want to know!

### 💻 Code (espn_service)
Fix bugs or add features to the Django service. Please include tests for any code changes.

---

## Development Setup

### 🐳 Docker (recommended — one command)

```bash
git clone https://github.com/pseudo-r/Public-ESPN-API.git
cd Public-ESPN-API

# Copy env file
cp .env.example espn_service/.env

# Start PostgreSQL, Redis, Django + Celery
docker compose up
```

API at **http://localhost:8000** · Swagger UI at **http://localhost:8000/api/schema/swagger-ui/**

---

### 🐍 Local (without Docker)

Prerequisites: Python 3.12+, PostgreSQL 14+, Redis 6+.

```bash
cd espn_service
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -e ".[dev]"

# Copy and edit env
cp ../.env.example .env
# Set DATABASE_URL and CELERY_BROKER_URL to your local values

python manage.py migrate
python manage.py runserver
```

---

## Pull Request Guidelines

1. **Branch off `Public-Api`** — this is the default documentation branch
2. **One concern per PR** — keep changes focused
3. **Write clear commit messages** — reference the endpoint or file you changed
4. **Add tests** for any `espn_service` code changes
5. **Update docs** if you change endpoints or add new features

### Commit message format

```
type: short description

- Bullet detail if needed
```

Types: `docs`, `feat`, `fix`, `test`, `chore`

---

## Documentation Style Guide

### Endpoint tables

Use the existing table format in sport-specific docs:

```markdown
| Endpoint | Method ID | Query Params |
| --- | --- | --- |
| `https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/teams` | `getTeams` | `page`, `limit`, `active` |
```

### Curl examples

- Use `https://` always (never `http://`)
- Add a `# comment` above each example describing what it does
- Use real, working slugs in examples (e.g., `nba` not `{league}`)

### File locations

| What | Where |
|------|-------|
| Sport-specific endpoints | `docs/sports/{sport}.md` |
| Global endpoint patterns | `docs/sports/_global.md` |
| Response JSON examples | `docs/response_schemas.md` |
| Site-wide docs | `README.md` |
| Change history | `CHANGELOG.md` |

---

## Code of Conduct

Be kind and respectful. This is a community resource — everyone is welcome.

---

## License

By contributing, you agree that your contributions will be licensed under the same [MIT License](LICENSE) as this project.
