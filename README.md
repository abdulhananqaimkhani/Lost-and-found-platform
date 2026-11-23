# Lost & Found Finder

A university final-year project. It collects public lost-and-found posts from lawful sources, extracts the item, place and date with a **hosted AI model**, and shows the reports on a map of Karachi.

- No accounts and no admin dashboard.
- The background worker calls OpenRouter's free Flint endpoint with a server-side API key. Ollama is available as an explicit configuration.
- Every coordinate comes from OpenStreetMap.

| Read this | For |
|---|---|
| [docs/setup-windows.md](docs/setup-windows.md) | Installing and running everything on Windows 11 |
| [docs/architecture.md](docs/architecture.md) | What was built, where, and how each guarantee is enforced |
| [docs/ai-pipeline.md](docs/ai-pipeline.md) | How a post becomes a report, and the measured AI accuracy |
| [docs/benchmarks/README.md](docs/benchmarks/README.md) | Model benchmark, matching evaluation, first live run |
| [docs/integrations/README.md](docs/integrations/README.md) | Which data sources are connected, which are not, and why |
| [docs/defense-notes.md](docs/defense-notes.md) | Likely viva questions, with answers |
| [docs/demo-script.md](docs/demo-script.md) | A rehearsed 10-minute demo |
| [PROJECT_PLAN.md](PROJECT_PLAN.md) | The original plan, progress log and decisions |

## Status

| Phase | State |
|---|---|
| 0 · Planning | Done |
| 1 · Frontend and design system | Done |
| 2 · Database and backend (PostgreSQL + PostGIS, FastAPI) | Done |
| 3 · AI extraction | The worker setup uses `recursive/flint-v1.0:free`. The benchmarks document the earlier local-model experiments. |
| 4 · Data collection connectors | Mastodon and the Global Voices Pakistan article feed are enabled. See the integrations guide for additional sources. |
| 5 · Geolocation and matching | Done: OSM gazetteer (52 places), Nominatim fallback, matching evaluated |
| 6 · Testing and finalization | Done, except a final Playwright run in API mode after the last UI changes (skipped: low memory) |

## Quick start

**Frontend only (demo data, no backend):**

```powershell
cd apps/web; npm install; npm run dev     # http://localhost:3000
```

**Full system:** see [docs/setup-windows.md](docs/setup-windows.md). In short:

```powershell
copy .env.example .env                    # for a fresh setup, then set passwords and OPENROUTER_API_KEY
docker compose up -d; uv sync; uv run alembic upgrade head
uv run python -m lf_worker seed-gazetteer
uv run uvicorn lf_api.main:app --host 127.0.0.1 --port 8000  # API
uv run python -m lf_worker check-ai                 # one synthetic extraction
uv run python -m lf_worker run                      # worker
cd apps/web; $env:NEXT_PUBLIC_DATA_SOURCE="api"; npm run build; npm run start -- --hostname 127.0.0.1
```

## Tests

```powershell
uv run pytest                              # 178 backend tests (needs the database running)
cd apps/web; npm test; npm run test:e2e    # frontend unit and end-to-end tests
```

The automated tests need no Ollama, no internet and no paid service.

## Repository layout

```
apps/web/            Next.js frontend
services/core/       lf_core: models, settings, text/redaction, queue, ingest, status, scoring
services/api/        lf_api: FastAPI public API
services/worker/     lf_worker: connectors, AI pipeline, geocoding, matching, scheduler, CLI
database/            Alembic migrations, gazetteer seed, demo dataset
config/sources.yaml  Which sources are enabled, with the terms check behind each decision
datasets/eval/       Labelled posts (AI evaluation) and labelled match pairs
scripts/             evaluate_model.py, evaluate_matching.py, geocode_places.py
docs/                Documentation and benchmark results
```

Map data © OpenStreetMap contributors (ODbL).
