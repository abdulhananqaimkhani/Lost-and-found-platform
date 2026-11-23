# Lost & Found Finder — Technical Implementation Plan (Phase 0)

> Prepared 2026-11-22. Approved and Phase 1 started on 2026-11-22.
> Every numeric estimate below is marked as an estimate and must be confirmed by measurement in the relevant phase.
> The local AI design below records the original implementation. See [Windows setup](docs/setup-windows.md) for the current hosted worker configuration.

## Progress log

| Phase | Status | Notes |
|---|---|---|
| 0 | Done | This document |
| 1 | Done (2026-11-22) | Frontend on demo data: 66 unit/component tests and 14 Playwright E2E tests passing. See "Phase 1 changes to the plan" below |
| 2 | Done (2026-11-22) | PostGIS in Docker (port 5442, because 5432 was taken by another project), hand-written Alembic migration, FastAPI. 20 frontend/backend parity cases. All 14 Playwright tests pass against the real API |
| 3 | Done (2026-11-22) | Ollama client (local-only, no cloud models), prefilter, structured extraction, grounding, date resolution, DB queue. 120 labelled posts plus 40 held-out posts. qwen2.5:3b and qwen2.5:1.5b benchmarked; the llama3.2:3b run and the held-out AI run were stopped by low memory and are still to do |
| 4 | Done (2026-11-22) | Connectors: Mastodon (live), RSS, X, Reddit, Bluesky (fixture-tested). News RSS feeds were **not** enabled because their terms forbid public reuse (docs/integrations). The first live run collected 22 posts, none of them a Karachi lost/found item |
| 5 | Done (2026-11-22) | Gazetteer of 52 OSM places, each hand-checked (4 wrong matches removed). Nominatim fallback with cache and ambiguity check. Matching evaluated on 30 labelled pairs |
| 6 | Done (2026-11-22) | Regression run, docs (setup, architecture, AI pipeline, integrations, defense notes, demo script) |

### Decisions taken during Phases 2–6 (all reversible)

| Decision | Why |
|---|---|
| A **Karachi coverage rule**: a post becomes a report only if it names Karachi, names a gazetteer place, or comes from a Karachi-only source | The first live run collected real lost/found posts from other countries. One named a person |
| Near-duplicates attach to the existing report instead of using a `duplicate_groups` table | One listing that says "posted in N places" |
| Posts about missing **people** are out of scope | They hold personal data about (often) children |
| `language` and `missing_fields` are computed in Python, not by the LLM | Deterministic and saves output tokens |
| Grounding v2: word-level copy check plus gazetteer fallback | The benchmark showed the model reorders or translates places; see docs/benchmarks |
| Match score: colour-conflict factor of 0.85 | The first matching evaluation showed conflicting colours were under-weighted |
| `PREFILTER_ENABLED` switch | The prefilter's held-out recall was 0.89 |

### Decisions taken by default (the §18 questions weren't answered yet; none affect Phase 1)

| Question | Default in use | Can change until |
|---|---|---|
| X API budget | Connector built and tested with fixtures, switched off | Phase 4 |
| Community submission form | Not built | Phase 4 |
| PostgreSQL hosting | Docker (with a WSL memory cap) | Start of Phase 2 |
| Resolution verification | Operator CLI command | Phase 2 |
| Reddit access request | Not drafted | Any time |

### Phase 1 changes to the plan

1. **New endpoint `GET /api/areas`.** The "Near <area>" filter and the distance reference need the gazetteer's named areas (`key`, `label`, `latitude`, `longitude`, `radius_m`).
2. **New list parameter `has_location=true|false`** on `GET /api/reports`. The map explorer lists located reports in the map view separately from reports that can't be mapped.
3. **Neighbourhood radius comes from the OSM bounding box** (half its diagonal, clamped to 400–2,500 m) rather than a fixed value. "Gulshan-e-Iqbal Block 10-A" (400 m) and "Gulshan-e-Iqbal Town" (2,500 m) are very different sizes.
4. **Co-located reports.** Reports with neighbourhood precision share their area's centre point, so they never separate by zooming. The map opens a list for such clusters. The backend should expect many reports at identical coordinates.
5. **Search must ignore redaction placeholders.** A masked phone number (`[phone •••]`) must not make a wallet post match a search for "phone". The backend search index must be built from text with the placeholders stripped (or from the unredacted text, never exposed).
6. **Geocoding ambiguity is real, not hypothetical.** During seeding, "Dolmen Mall Clifton" matched a restaurant inside the mall and "Federal B Area" matched an unrelated building. This supports the plan for an alias table, an ambiguity check and `needs_review` (§6).

---

## 0. Workspace inspection results

| Item | Finding | Consequence |
|---|---|---|
| Project folder | Empty, not a git repository | Fresh monorepo; `git init` at the start of Phase 1 |
| OS | Windows 11 Pro 22631 | Matches target environment |
| CPU | Intel Core i5-6300U, **2 cores / 4 threads** (2015 laptop class) | All AI inference is **CPU-only** and will be slow (see §3, §13) |
| RAM | **7.9 GB** | Memory budget is the tightest constraint of the whole project |
| GPU | Intel HD 520 (integrated), no NVIDIA/CUDA | Ollama cannot use GPU acceleration |
| Free disk (C:) | 35.6 GB | Sufficient (models ~2–3 GB each, Docker images ~1 GB) |
| Node.js | v24.21.0 | OK for Next.js |
| Python | 3.14.7 only | Some geo/DB wheels may lag on 3.14 → pin project to **Python 3.12** via `uv` (see §14) |
| Git | 2.56 | OK |
| Docker Desktop | 29.8.1 installed, **daemon not running**, WSL2 backend, no `.wslconfig` | Must start it and cap WSL memory |
| Ollama | **Not installed** | Install natively (§14) |
| PostgreSQL client | Not installed | Not required (runs in Docker) |

---

## 1. Architecture overview

Three logically independent systems that share one database and one contract (the normalized post schema).

```
                        ┌─────────────────────────── Your Windows PC ────────────────────────────┐
                        │                                                                        │
 External sources       │   ┌──────────────── WORKER (Python, background) ────────────────┐      │
 (authorized only)      │   │                                                              │      │
 ┌──────────────┐ HTTPS │   │  A. Collection Engine          B. AI Intelligence Engine     │      │
 │ RSS feeds    │──────────▶│  connectors/ (one per source)   pipeline/                    │      │
 │ X API (paid) │──────────▶│   └─ normalize → NormalizedPost  ├─ stage 1: keyword filter  │      │
 │ Reddit (*)   │──────────▶│   └─ dedupe (id/url/hash)        ├─ stage 2: Ollama extract ─┼─┐    │
 │ Bluesky/Mast.│──────────▶│   └─ insert Source + Job         ├─ validate & ground        │ │    │
 │ Permitted web│──────────▶│                                  ├─ geocode (gazetteer→OSM)  │ │    │
 └──────────────┘       │   │  APScheduler  ◀── DB job queue ──├─ duplicate grouping       │ │    │
                        │   │                                  └─ match scoring            │ │    │
 Nominatim (OSM) ◀──────────│──────────── rate-limited 1 req/s, cached ────────────────────┘ │    │
                        │   └──────────────────────────────┬───────────────────────────────┘ │    │
                        │                                  │ SQLAlchemy                      ▼    │
                        │                                  ▼                       ┌──────────────┐│
                        │   ┌──────────────────────────────────────────┐           │ Ollama       ││
                        │   │ PostgreSQL 16 + PostGIS  (Docker :5432)  │           │ 127.0.0.1:   ││
                        │   └──────────────────────────────────────────┘           │ 11434 (local ││
                        │                                  ▲                       │ only)        ││
                        │                                  │ SQLAlchemy            └──────────────┘│
                        │   ┌──────────────────────────────┴───────────┐                          │
                        │   │ C. FastAPI backend (:8000)               │  ◀── never talks to Ollama│
                        │   │   /api/* public (read + suggestions)     │                          │
                        │   │   /internal/* service-token only         │                          │
                        │   └──────────────────────────────▲───────────┘                          │
                        │                                  │ HTTP (proxied /api/*)                │
                        │   ┌──────────────────────────────┴───────────┐                          │
                        │   │ Next.js frontend (:3000)                 │──▶ OSM tiles (browser)   │
                        │   └──────────────────────────────────────────┘                          │
                        └────────────────────────────────────────────────────────────────────────┘
 (*) Reddit requires manual API approval — see §4.
```

**Key design rules**

1. The **browser never talks to Ollama or to any social platform**. It talks only to Next.js, which proxies `/api/*` to FastAPI.
2. **Ollama listens on 127.0.0.1 only** and is called only by the worker.
3. **The API never calls external sources**; it only serves what the worker has already processed. If every external source is down, the website still works.
4. **Connectors are plugins**: adding a platform = adding one file that implements `Connector.fetch() -> list[NormalizedPost]`. AI, DB, and frontend are untouched.
5. **Configuration over code**: models, keywords, schedules, budgets, and enabled sources are environment variables or `config/sources.yaml`.

---

## 2. How the components communicate

| From → To | Mechanism | Notes |
|---|---|---|
| Browser → Next.js | HTTP | Pages, React Server Components |
| Next.js → FastAPI | HTTP via Next `rewrites` (`/api/*` → `http://localhost:8000/api/*`) | Same origin for the browser → simple CORS; in production the target is an env var |
| FastAPI ↔ PostgreSQL | SQLAlchemy 2.0 (sync) + psycopg 3 + GeoAlchemy2 | Connection pool; all queries parameterized |
| Worker ↔ PostgreSQL | Same shared `lf_core` package (models, repositories) | Queue = `processing_jobs` table claimed with `SELECT … FOR UPDATE SKIP LOCKED` |
| Worker → Ollama | `httpx` → `POST http://127.0.0.1:11434/api/chat` (and `/api/embed`) | Timeouts, circuit breaker, no cloud fallback |
| Worker → external sources | `httpx` per connector | Retry with exponential backoff + jitter, honours `Retry-After`/rate-limit headers |
| Worker → Nominatim | `httpx`, 1 req/s, identifying User-Agent, results cached in DB | Per OSM usage policy |

**Deployment-readiness (designed now, built later):** the worker accesses the database through a `WorkerGateway` interface. Locally it uses `DirectDbGateway`. For online deployment (API + DB on a server, worker + Ollama staying on your PC), an `HttpGateway` calls `/internal/*` endpoints over HTTPS with a bearer service token. This means the worker only makes **outbound** connections, so your PC and Ollama are never exposed to the internet. The `/internal` ingestion endpoints are built in Phase 4. The `HttpGateway` is a Phase 6 stretch goal.

---

## 3. How the Ollama engine operates locally

### 3.1 Runtime

- Ollama runs **natively on Windows** as a background service, bound to `127.0.0.1:11434` (default; we will *not* set `OLLAMA_HOST=0.0.0.0`).
- Recommended Ollama env for this laptop: `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KEEP_ALIVE=10m`.
- Worker env (no model name is hardcoded anywhere):

```env
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen2.5:3b            # chosen after Phase 3 benchmark
OLLAMA_EMBED_MODEL=nomic-embed-text # optional, Phase 5
OLLAMA_TIMEOUT_SECONDS=180
OLLAMA_NUM_CTX=2048
AI_MAX_JOBS_PER_MINUTE=4
```

### 3.2 Request shape

`POST /api/chat` with:
- `format`: the **JSON Schema** generated from our Pydantic `ExtractionResult` model (Ollama structured outputs constrain decoding to the schema).
- `options`: `temperature: 0`, `num_ctx: 2048`, `num_predict: ~350`, `seed` fixed for reproducibility.
- `think: false` for model families that support a thinking mode (e.g. Qwen3.x), because reasoning tokens would multiply CPU time.
- `stream: false`, `keep_alive: "10m"`.
- Messages: system prompt (rules + output contract), 4–6 short few-shot examples (English, Roman Urdu, Urdu, tricky negatives), then the post text (truncated to ~1,500 characters) plus its `published_at`.

### 3.3 Model selection (benchmark, not assumption)

Hardware reality: a 2-core CPU with no GPU. **Estimate (to be measured):** a 3B–4B model at Q4 quantization will generate roughly 3–8 tokens/s here. With ~600 prompt tokens and ~150 output tokens, that works out to about **20–60 seconds per post**. That speed is acceptable for a background queue handling tens to low hundreds of candidate posts per day. It is too slow for anything interactive, which is one reason the website never calls the model directly.

Benchmark candidates for Phase 3 (exact tags verified against the Ollama library at benchmark time):

| Candidate | Why |
|---|---|
| `qwen2.5:3b` | Your suggested baseline; good JSON adherence |
| `qwen2.5:1.5b` | Speed fallback if 3B is too slow on this CPU |
| `llama3.2:3b` | Well-known baseline for comparison |
| A newer 4B-class model (e.g. Qwen3.5 4B or Gemma 4 E4B, if available in Ollama) | 2026 guides rank these as the strongest CPU-friendly small models; must verify RAM/speed here |

Each model is scored on the labelled dataset (§8) for: JSON validity rate, classification macro-F1, field accuracy, hallucination rate, p50/p95 latency, and peak RAM. The chosen model is simply the one that maximizes F1 subject to a latency/RAM budget. The model goes in `.env` and the result goes in `docs/benchmarks/`.

### 3.4 Failure handling

- **Ollama down / model not pulled:** a health check (`GET /api/tags`) fails, so a circuit breaker opens. AI jobs stay `PENDING` and **do not consume retry attempts**. The worker re-checks every 60 s and logs a clear message (`Ollama unreachable` or `model X not found: run "ollama pull X"`). **There is no cloud fallback, ever.**
- **Invalid JSON / schema violation:** one repair retry with a stricter reminder. If it fails again, the job becomes `FAILED` (or `needs_review`) with the raw output stored for debugging.
- **Timeout:** the job is retried with exponential backoff (`available_at = now + 30s·2ⁿ ± jitter`, max 5 attempts).

---

## 4. How external posts are collected

### 4.1 Connector contract

```python
class Connector(Protocol):
    platform: str                      # "RSS", "X", "REDDIT", ...
    def is_configured(self) -> bool    # credentials present? enabled in config?
    def fetch(self, cursor: Cursor | None, limits: Limits) -> FetchResult  # posts + next cursor
```

Every connector returns `NormalizedPost` (the schema in your brief: `source_platform`, `source_id`, `source_url`, `text`, `published_at`, `collected_at`) plus an optional `raw_meta` dict for platform specifics. Connectors that are not configured are **skipped with a log line**. They never crash the worker.

### 4.2 Scheduling and limits

- APScheduler runs one interval job per enabled source (`max_instances=1`, `coalesce=True`), configured in `config/sources.yaml`:

```yaml
sources:
  rss:
    enabled: true
    interval_minutes: 30
    feeds: []            # each feed added only after its terms are checked
  x:
    enabled: false        # turns on only when X_BEARER_TOKEN is set
    interval_minutes: 120
    max_posts_per_run: 50
    monthly_budget_usd: 5.00
    queries: ["(wallet OR phone OR CNIC) (lost OR found) Karachi", "\"kho gaya\" Karachi", "\"mil gaya\" Karachi"]
keywords:
  english: [lost, found, missing, misplaced, "left behind"]
  roman_urdu: ["kho gaya", "kho gayi", "gum ho gaya", "mil gaya", "mila hai", "missing hai", "gir gaya"]
  urdu: ["گم ہو گیا", "کھو گیا", "مل گیا"]
```

- Each run creates a `collection_runs` row (started/completed, status, counts, errors).
- **Incremental fetching:** each source stores a cursor (`since_id`, last `published_at`, ETag/Last-Modified for RSS) so the same item is not fetched twice.
- **Dedup at ingest:** unique `(platform, external_post_id)`, unique canonical URL, and a normalized-text hash catch cross-posts.
- **Backoff:** `429` and `5xx` responses trigger exponential backoff with jitter and honour `Retry-After` / `x-rate-limit-reset`. A connector that fails N runs in a row is paused for a cooldown period. Other sources are unaffected.
- **Cost controls (paid APIs):** a hard `max_posts_per_run` and a `monthly_budget_usd` ledger computed from `collection_runs` (records × unit price from config). The connector refuses to run once the budget is reached.

### 4.3 Source-by-source reality check

The most important finding of Phase 0: **lawful, free access to the places where Karachi lost-and-found posts actually live is very limited.**

| Source | Access status (researched 2026-11-22) | Plan |
|---|---|---|
| **RSS feeds** | Free, lawful for feeds published for syndication | **Implement first.** Pakistani news site feeds and Google News RSS search queries. Expect *low* volume of genuine lost/found items (mostly found children, documents, appeals). Each feed's terms are checked before enabling. |
| **X (Twitter) API** | Now **pay-per-use**: credits bought in advance; recent search is billable per post returned; the official pricing table lists Posts: Read at about **$0.005/post**, with a monthly cap on post reads. Rates must be confirmed in the Developer Console. | **Implement connector + mocked tests.** Activates only when `X_BEARER_TOKEN` is set. Default budget cap is tiny (e.g. $5/month ≈ 1,000 posts). The X Developer Policy must be followed, including honouring deletions and attribution. |
| **Reddit Data API** | Since Nov 2025 the **Responsible Builder Policy** requires manual approval for new API access; reported rejections are common. | Implement connector behind config. **Only activate if approval is granted.** Apply early (non-commercial academic use, r/karachi, r/pakistan). |
| **Bluesky (AT Protocol)** | Open protocol; `app.bsky.feed.searchPosts` exists. Unauthenticated access to the public AppView is inconsistent (403s reported), and a free app password may be required. | Optional connector using a free account app password. Expect low Karachi volume. |
| **Mastodon** | Public hashtag timelines via open API on instances that allow it | Optional. Low volume. |
| **Permitted public websites** | Case by case | Generic `WebPageConnector` restricted to an **allowlist**, checking `robots.txt` (`urllib.robotparser`) and ToS before enabling. No candidate sites confirmed yet; we will not invent any. |
| **Facebook Groups** | The **Groups API was removed in Graph API v19 (April 2024)**. There is no third-party read access to group posts. | **Not implementable.** Documented in `docs/integrations/facebook.md`. No scraping, no login automation. |
| **Facebook Pages** | Reading public pages you don't manage requires the *Page Public Content Access* feature and Meta App Review (business verification) | Research doc only. Realistically out of reach for a student project. |
| **Instagram** | Graph API works only for Business/Creator accounts; hashtag search requires app review and is limited per account | Research doc only. |

**Consequence for the FYP demo:** without paid X credits or Reddit approval, real data volume will be low. See the open question in §15 about adding a lawful **"Submit a public post" connector**, where a visitor pastes the text and URL of a public post they wrote or are permitted to share. Those submissions would go through the same AI pipeline and be labelled as community-submitted.

---

## 5. How information extraction works

### 5.1 Pipeline

```
NormalizedPost
   │
   ▼ Stage 0  ingest dedup (platform+id, canonical URL, text hash) ── duplicate → attach to existing report
   │
   ▼ Stage 1  Python keyword pre-filter (cheap, high-recall)
   │           • normalize: lowercase, Unicode NFKC, collapse Roman-Urdu spelling variants (gya/gaya, mila/milla)
   │           • score = intent keywords (lost/found/kho/mil) × item lexicon (wallet/batwa/phone/mobile/CNIC/bag/keys…)
   │           • negative patterns ("lost my mind", "lost the match", "found this restaurant", "for sale")
   │           • below threshold → stored as REJECTED_PREFILTER, never sent to the LLM, raw text purged after 7 days
   │
   ▼ Stage 2  Ollama: one call = classification + extraction (structured JSON)
   │
   ▼ Stage 3  Python validation & grounding (the anti-hallucination layer)
   │
   ▼ Stage 4  Report created/updated → GEOCODE job → DEDUPE/MATCH job
```

A single LLM call does both classification and extraction because on this CPU each call costs tens of seconds. Pre-filter recall is measured on the labelled set. If it drops relevant posts, the thresholds are loosened, since a false negative there is permanent.

### 5.2 Output schema (Pydantic, also sent to Ollama as `format`)

```python
class ExtractionResult(BaseModel):
    is_relevant: bool
    report_type: Literal["LOST", "FOUND", "IRRELEVANT", "UNCERTAIN"]
    item_name: str | None
    item_category: Literal["WALLET","PHONE","DOCUMENT_ID","BAG","KEYS","JEWELLERY",
                           "ELECTRONICS","PET","VEHICLE","PERSON","CLOTHING","OTHER"] | None
    description: str | None
    attributes: ItemAttributes            # colour, brand, model — each nullable
    location_text: str | None             # VERBATIM span copied from the post
    city: str | None
    date_expression: str | None           # VERBATIM span, e.g. "kal shaam", "22 Nov"
    language: Literal["ENGLISH","ROMAN_URDU","URDU","MIXED","OTHER"]
    confidence: float                     # 0..1, model self-report
    missing_fields: list[str]             # what the post does not say
```

### 5.3 Grounding rules enforced in Python (not trusted to the model)

| Field | Rule |
|---|---|
| `location_text` | Must fuzzy-match a span of the original text (`rapidfuzz.partial_ratio ≥ 85` after normalization). Otherwise it is set to `null` and the report is flagged `needs_review`. |
| `city` | Kept only if it appears in the text, **or** the location matches the Karachi gazetteer, in which case `city_source = "INFERRED_FROM_GAZETTEER"` is stored explicitly. |
| `incident_date` | **The LLM never outputs a date.** It outputs a verbatim `date_expression`. Python resolves it against `published_at` ("aaj" → same day, "kal" → previous day, "parson" → ±2 days, explicit dates parsed). The result is stored as `incident_date` + `date_precision` (EXACT / APPROXIMATE / UNKNOWN). |
| Coordinates | Never produced by the LLM. Only the geocoder (§6) produces coordinates. |
| `item_name` | Must share at least one token with the text or be a lexicon synonym (e.g. "batwa" → wallet). Otherwise it is flagged. |
| `confidence` | The stored `confidence_score` combines the model's self-report with deterministic signals (schema completeness, grounding checks passed, pre-filter score). We document that LLM self-reported confidence is poorly calibrated, which is a good academic talking point. |

Reports with `UNCERTAIN`, failed grounding, or a combined confidence below threshold get status `UNVERIFIED` and a visible "needs review" indicator in the UI.

### 5.4 Privacy minimization

Lost-ID posts often contain CNIC numbers, phone numbers, and names. Before display, `original_text` is passed through a redaction filter: 13-digit CNIC patterns are masked (`42101-•••••••-1`) and phone numbers are masked. The **original source link is always shown**, so people contact each other through the original platform. Author names and handles are not stored. Irrelevant posts' text is purged after 7 days.

---

## 6. How location extraction and geocoding work

### 6.1 Flow

```
location_text ("gulshan blk 10-A") + city
  │
  ▼ normalize & alias      "Gulshan-e-Iqbal Block 10-A"   (alias table: Gulshan, G-e-I, Gulshan e Iqbal …;
  │                                                        DHA Ph 6 → DHA Phase 6; Lucky One → LuckyOne Mall)
  ▼ local gazetteer        `places` table seeded ONCE from OpenStreetMap (Overpass/Nominatim) for Karachi:
  │                        neighbourhoods, blocks, major malls, universities, landmarks — with OSM id + ODbL attribution
  ▼ cache                  `geocode_cache` (query_norm → result, incl. negative results)
  ▼ Nominatim (fallback)   countrycodes=pk, viewbox=Karachi bbox, bounded=1, 1 req/s, User-Agent identifying the app
  ▼ precision mapping      OSM class/type → POI | STREET | NEIGHBOURHOOD | CITY | UNKNOWN (+ radius_m)
  ▼ ambiguity check        multiple candidates > 2 km apart & nothing disambiguates → do not map, needs_review
```

Every coordinate is traceable to an OSM object or a cached geocoder response. Nothing is invented. Placeholder or seed coordinates do not exist in the system.

### 6.2 Precision and how it is displayed

| Precision | Example | Typical radius | Map display |
|---|---|---|---|
| `POI` | "LuckyOne Mall", "NED University main gate" | ~100 m | Pin |
| `STREET` | "Tariq Road" | ~400 m | Pin with "approximate" ring |
| `NEIGHBOURHOOD` | "Bahadurabad", "Gulshan Block 10A" | ~800–1,500 m | **Translucent circle, no pin** |
| `CITY` | "Karachi" only | — | **Not on map**, list only ("Location: Karachi (not mapped)") |
| `UNKNOWN` | No location in post | — | List only |

This satisfies "do not show a misleading exact marker for an approximate neighbourhood location."

The provider sits behind a `Geocoder` interface (`GazetteerGeocoder`, `NominatimGeocoder`, `ChainGeocoder`), so Photon, a self-hosted Nominatim, or a commercial provider can be swapped in without touching the pipeline. Expanding to Lahore or Islamabad means seeding their gazetteer and adding the city to config.

---

## 7. How the database stores and retrieves reports

### 7.1 Entities (PostgreSQL 16 + PostGIS 3, Alembic migrations in `database/migrations`)

```
duplicate_groups 1───* reports 1───* sources 1───* processing_jobs
                         │  1
                         ├───* status_suggestions
                         ├───* status_changes            (audit log)
                         └───* report_matches (a_id, b_id)
collection_runs            places (gazetteer)            geocode_cache
```

**reports**
`id (uuid)`, `report_type (LOST|FOUND)`, `item_name`, `item_category`, `description`, `attributes jsonb`, `location_text`, `city`, `city_source`, `latitude`, `longitude`, **`geog geography(Point,4326)` generated from lat/lng (GiST index)**, `location_precision`, `location_radius_m`, `incident_date`, `date_precision`, `reported_at` (earliest source `published_at`), `status (ACTIVE|POTENTIAL_MATCH|RESOLVED|DISPUTED|STALE|UNVERIFIED)`, `confidence_score`, `needs_review`, `language`, `extraction_model`, `duplicate_group_id → duplicate_groups`, `is_demo bool`, `search_tsv tsvector` (GIN), `created_at`, `updated_at`.

**sources**
`id`, `platform`, `external_post_id`, `original_url`, `canonical_url`, `original_text`, `text_hash`, `published_at`, `collected_at`, `prefilter_score`, `prefilter_result`, `report_id → reports (nullable)`, `collection_run_id`.
Unique `(platform, external_post_id)`, unique `canonical_url`, index on `text_hash`.

**processing_jobs**
`id`, `source_id → sources`, `job_type (EXTRACT|GEOCODE|MATCH)`, `status (PENDING|RUNNING|DONE|FAILED)`, `retry_count`, `available_at`, `locked_at`, `locked_by`, `error_message`, `processed_at`, `created_at`. Partial index on `(status, available_at) WHERE status='PENDING'`.

**collection_runs**
`id`, `platform`, `started_at`, `completed_at`, `status`, `records_collected`, `records_processed`, `errors jsonb`, `cost_units`, `cursor jsonb`.

**status_suggestions**
`id`, `report_id`, `suggested_status (RESOLVED|INACCURATE)`, `reason` (≤ 500 chars), `fingerprint_hash` (HMAC of IP + daily salt, so no raw IPs are stored), `created_at`, `review_status (PENDING|ACCEPTED|REJECTED)`. Unique `(report_id, fingerprint_hash, suggested_status)`.

**status_changes** (audit): `report_id`, `from_status`, `to_status`, `reason`, `actor (SYSTEM|OPERATOR_CLI|SOURCE_EVIDENCE)`, `created_at`.

**report_matches**: `lost_report_id`, `found_report_id`, `score`, `breakdown jsonb`, `created_at`. Unique pair.

**places**: `id`, `name`, `aliases text[]`, `city`, `osm_type`, `osm_id`, `precision`, `geog`, `radius_m`.
**geocode_cache**: `query_norm` (pk), `provider`, `result jsonb`, `created_at`.

### 7.2 Retrieval

- **Bounding-box map query:** `WHERE geog && ST_MakeEnvelope(:w,:s,:e,:n,4326)::geography` (uses GiST).
- **Distance sort/filter:** `ST_DWithin(geog, :point, :radius_m)` and `ORDER BY geog <-> :point`.
- **Text search:** `tsvector` with the `simple` configuration (language-agnostic, works for Roman Urdu) plus `pg_trgm` similarity on `item_name` for typos ("wallat").
- **De-duplicated listing:** one row per `duplicate_group_id` (canonical report), with a `source_count`.
- All writes for one pipeline step happen in **one transaction** (e.g. report insert + source link + next job enqueue).

---

## 8. API design (FastAPI)

### Public (read-only + suggestions, rate-limited)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/reports` | List. Query: `q, type, category, status, area, city, date_from, date_to, bbox, near_lat, near_lng, radius_km, has_location, sort=newest|oldest|distance|relevance, page, page_size≤100`. Returns `{items, total, page, page_size}` |
| GET | `/api/reports/{id}` | Detail incl. all sources (redacted text), precision, status, suggestion counts |
| GET | `/api/map/reports` | Compact GeoJSON `FeatureCollection` for `bbox` + same filters, mapped reports only, capped at 2,000 features |
| GET | `/api/reports/{id}/matches` | Potential matches with score breakdown |
| POST | `/api/reports/{id}/resolution-suggestions` | Body `{suggested_status, reason}`; returns 202. Never changes status directly |
| GET | `/api/categories` | Categories with counts |
| GET | `/api/areas` | Named areas from the gazetteer, for the "Near" filter (added in Phase 1) |
| GET | `/api/stats` | Totals by type/status/source, last collection time |
| GET | `/api/health` | DB status, last worker heartbeat, queue depth (no secrets) |

### Internal (`/internal/*`, `Authorization: Bearer $SERVICE_TOKEN`, disabled when token unset)

`POST /internal/ingest` (batch of NormalizedPost), `POST /internal/jobs/claim`, `POST /internal/jobs/{id}/complete`, `POST /internal/jobs/{id}/fail`. These support the future remote-worker deployment.

### Cross-cutting

- Errors use one shape: `{"error": {"code": "...", "message": "...", "details": ...}}` with correct status codes (400/404/422/429/500).
- Rate limiting via `slowapi`: e.g. 120 req/min per client for reads, 5 suggestions/hour per fingerprint.
- CORS allowlist from env (only the frontend origin).
- `pydantic-settings` for config. Secrets live only in `.env`, which is git-ignored. `/api/health` never echoes config.
- Structured JSON logs with request id and timing.

---

## 9. How the frontend map receives and displays reports

### 9.1 Stack

Next.js (App Router, TypeScript), Tailwind CSS, shadcn/ui (customized), Lucide, **react-leaflet + Leaflet**, `leaflet.markercluster`, OpenStreetMap tiles, TanStack Query (fetching/caching), Zustand (tiny shared selection state). Versions are verified for compatibility at install time.

### 9.2 Pages

| Route | Content |
|---|---|
| `/` Map Explorer | Search, LOST/FOUND toggle, category chips, area and date filters, clear-filters, result count, split view (list panel + map); bottom-sheet list on mobile |
| `/reports` Explore | Full listing, all filters, sort by newest or by distance from a user-chosen point ("use my location" or click on map) |
| `/reports/[id]` Detail | Item info, type and status badges, description, approximate-location mini map, original date, collection date, sources with "Open original post", potential matches, "Suggest as resolved / report inaccurate" dialog |
| `/about` How it works | Collection, supported sources, AI extraction, geocoding, what precision means, how to report problems, privacy |

### 9.3 Data flow and synchronization

1. **Filters live in the URL** (`?type=LOST&cat=WALLET&q=black`), so views are shareable and back/forward works.
2. The map's `moveend` event (debounced 300 ms) updates the bbox. TanStack Query then fetches `/api/map/reports` (markers) and `/api/reports` (list, filtered to the same bbox when "search this area" is on).
3. **One Zustand store holds `selectedId` and `hoveredId`:**
   - List item click → `selectedId` set → map `flyTo` (zoom 16 for POI, 14 for neighbourhood circle) → popup opens → list item highlighted.
   - Marker click → `selectedId` set → list scrolls the item into view and highlights it.
   - Hover (desktop only, detected via `matchMedia('(hover: hover)')`) → compact preview card. On touch devices, tap opens the preview and a second tap goes to the details page.
4. **Marker styling:** LOST and FOUND differ in **both colour and glyph** (colour-blind safe). Approximate locations render as translucent circles sized to `location_radius_m`. Clusters show counts with a LOST/FOUND split.
5. Leaflet is loaded with `next/dynamic({ ssr: false })` because it needs `window`.
6. Map centred on Karachi; zoom/pan limits keep it usable.

### 9.4 Phase 1 mock data (honesty rule)

Phase 1 uses a mock adapter (`NEXT_PUBLIC_DATA_SOURCE=mock`) behind the same TypeScript API client that later talks to FastAPI. Mock reports are **obviously synthetic**: `source_platform: "DEMO"`, source URLs on the reserved `example.invalid` domain, and a "Demo data" banner. Their coordinates come from **real OSM lookups of real Karachi place names**, so the map looks realistic without inventing geography. Demo records carry `is_demo=true` and are excluded when `SHOW_DEMO_DATA=false`.

### 9.5 Design direction

The look is a calm, map-first discovery tool, not an admin template. The canvas uses neutral warm greys with one restrained accent. LOST uses a coral/vermilion family and FOUND uses a teal/green family. Type is a crisp sans with tabular figures for dates and counts. Edges are thin borders and soft shadows only where elements float over the map. Cards are dense and scannable, showing item, badge, location with a precision icon, relative date, and source icon. Motion is limited to map fly-to and list highlight transitions. The detailed design system is produced at the start of Phase 1.

---

## 10. Status management (no accounts, no admin dashboard)

| Status | How a report gets it |
|---|---|
| `UNVERIFIED` | Low confidence, `UNCERTAIN`, or grounding failed |
| `ACTIVE` | Confident extraction. The UI still says "Imported from X — not independently verified" |
| `POTENTIAL_MATCH` | A match score ≥ threshold exists (§11) |
| `STALE` | Automatic after N days (default 45) with no new source activity |
| `RESOLVED` | **Only** via (a) source evidence (a later post from the same source or thread classified as "found/returned") or (b) the operator running a CLI review command |
| `DISPUTED` | Operator CLI review when suggestions conflict or "inaccurate" suggestions are confirmed |

- Anonymous "Suggest as resolved" / "Report inaccurate" requests are **stored as suggestions only**. The public status does not change. The detail page shows "3 visitors suggested this is resolved."
- Anti-spam: one suggestion per fingerprint per report per type, a per-fingerprint hourly limit, a reason length cap, and an optional honeypot field.
- Every status change is written to `status_changes`.
- The operator review is a **command-line tool** (`python -m lf_worker review-suggestions`), not a web dashboard (see open question in §15).

---

## 11. Matching engine (secondary, Phase 5)

1. **Candidates (SQL):** opposite type, same or related category, `ST_DWithin(5 km)` (or same city when unmapped), incident dates within ±14 days.
2. **Score** (weights configurable, documented):
   `0.30·category + 0.30·text_similarity + 0.20·distance_decay + 0.10·date_proximity + 0.10·attribute_overlap (colour/brand)`
   - `text_similarity`: `rapidfuzz` token-set ratio, upgraded to cosine similarity of **local Ollama embeddings** (`nomic-embed-text`) if the benchmark shows a benefit.
3. **Score ≥ 0.65** creates a `report_matches` row and sets `POTENTIAL_MATCH`. The UI shows "Possible match · same category · 1.2 km apart · 2 days apart" and never "confirmed."
4. Evaluated on a small hand-labelled set of match / non-match pairs.

**Duplicate detection** (same incident posted multiple times) uses the same building blocks with stricter thresholds:
- Exact duplicates (same id, canonical URL, or text hash) → the new source attaches to the **same report**.
- Near duplicates (same type, ≤ 7 days, text similarity ≥ 0.9 or embedding cosine ≥ 0.92, compatible location) → separate reports in the **same duplicate group**. The list shows one card: "Also posted in 2 other places."

---

## 12. Testing without spending money

| Layer | Tooling | What is tested |
|---|---|---|
| AI client | `pytest` + `respx` (mocks `httpx`) | Valid JSON, invalid JSON → repair → failure, timeouts, Ollama down → circuit breaker, no cloud fallback |
| Extraction logic | `pytest` with recorded/fixture model outputs | English, Roman Urdu, Urdu, irrelevant, missing fields stay `null`, grounding rejects hallucinated locations, date resolution |
| Pre-filter | `pytest` on labelled set | Recall/precision of stage 1 |
| Geocoding | `pytest` + `respx` Nominatim fixtures + gazetteer | Alias normalization, precision mapping, ambiguity, cache, rate limiter |
| Database | `pytest` against a real PostGIS **test database** (`lf_test`, same container) with migrations applied | Constraints, unique dedup keys, bbox/distance queries, transactions |
| Queue | `pytest` | Claim with SKIP LOCKED, retries/backoff, poison jobs |
| Connectors | `pytest` + recorded fixtures (RSS XML, X API JSON shaped per official docs) | Normalization, cursors, rate-limit handling, budget cap |
| API | `pytest` + FastAPI `TestClient` | Filters, pagination, bbox, errors, rate limits, internal auth |
| Frontend unit | Vitest + React Testing Library | Filter state ↔ URL, selection store, cards |
| Frontend E2E | Playwright (against mock adapter) | **Map ↔ list synchronization**, hover vs click, mobile layout |
| **AI evaluation** | `scripts/evaluate_model.py` with **live local Ollama** (manual, not CI) | Precision/recall/F1 per class, macro-F1, confusion matrix, field accuracy, hallucination rate, JSON validity, latency, RAM. Compared across models and against a keyword-only baseline |

**Labelled dataset** (`datasets/eval/posts.jsonl`, target 150–200 rows): each row has `text`, gold labels, and an `origin` field. Initially every row is `SYNTHETIC_HANDWRITTEN`, written by us to cover English, Roman Urdu, Urdu, mixed language, and tricky negatives ("lost the match", "found the best biryani", "selling phone", news about "missing funds"). Later, real collected posts that we annotate are added with `origin: COLLECTED` and their source reference. Reported metrics always state which subset they were measured on. **No accuracy claim is made without this evaluation.**

The automated suite needs no paid API and no running Ollama. It needs only Docker for the test database.

---

## 13. Project folder structure

```
lost-and-found/
├── apps/
│   └── web/                      # Next.js app (pages, components, lib/api client, mock adapter, tests)
├── services/
│   ├── core/                     # lf_core: settings, SQLAlchemy models, repositories, shared Pydantic contracts,
│   │                             #          geo utilities, text normalization (used by api AND worker)
│   ├── api/                      # lf_api: FastAPI app, routers/, schemas/, rate limiting, tests/
│   └── worker/                   # lf_worker: scheduler, queue, connectors/, pipeline/ (prefilter, ollama_client,
│                                 #            extraction, grounding, geocoding/, dedupe, matching), cli, tests/
├── database/
│   ├── migrations/               # Alembic env + versions
│   └── schema/                   # ERD, schema docs, gazetteer seed (OSM-derived, with attribution)
├── config/
│   └── sources.yaml              # enabled sources, schedules, keywords, budgets
├── datasets/
│   └── eval/                     # labelled evaluation posts + match pairs
├── scripts/                      # evaluate_model.py, benchmark_models.py, seed_gazetteer.py, dev helpers
├── docs/
│   ├── architecture.md  integrations/  benchmarks/  setup-windows.md  ai-pipeline.md  defense-notes.md
├── docker-compose.yml            # postgis/postgis:16-3.x  (+ optional api/worker profiles later)
├── pyproject.toml                # uv workspace (core, api, worker)
├── .env.example
├── README.md
└── PROJECT_PLAN.md
```

`services/core` is the one addition to your proposed structure. Without it, the API and the worker would each need their own copy of the database models.

---

## 14. Development phases

Each phase ends with: what was built, files changed, the **actual** test output, open problems, and the next phase. I wait for your approval before starting the next phase.

| Phase | Scope | Exit criteria |
|---|---|---|
| **1 · Frontend & design system** | git init, monorepo skeleton, Next.js + Tailwind + shadcn, design tokens, all 4 pages, Leaflet map with clustering, precision circles, hover/click previews, map↔list sync, URL filters, mobile bottom sheet, mock adapter with clearly labelled demo data | All interactions work on mock data; Vitest + Playwright sync tests pass; Lighthouse sanity check on mobile layout |
| **2 · Database & backend** | docker-compose PostGIS, uv workspace, `lf_core` models, Alembic migrations, FastAPI endpoints, search/filter/bbox/distance, rate limiting, error format, frontend switched to real API | Migrations apply cleanly; API tests pass; frontend works against the API |
| **3 · Local AI engine** | Ollama client, pre-filter, prompt + schema, grounding, date resolution, queue processing, labelled dataset, model benchmark | Mocked tests pass; benchmark report with real measured F1/latency/RAM per model; model chosen in `.env` |
| **4 · Data collection** | Connector framework, RSS (live), X/Reddit/Bluesky connectors (fixture-tested, credential-gated), scheduling, cursors, backoff, budgets, `/internal` ingest endpoints, integration research docs | Real RSS posts collected end-to-end into the map; other connectors pass fixture tests and are documented |
| **5 · Geolocation & matching** | Gazetteer seeding from OSM, Nominatim fallback with cache and rate limiting, precision display, duplicate grouping, matching engine, embeddings (if they help) | Geocoding tests pass; real collected reports appear with correct precision; match evaluation numbers recorded |
| **6 · Testing & finalization** | Full regression, bug fixes, README and Windows setup guide, demo script, defense notes, optional `HttpGateway` | Clean setup from scratch following README; all suites green; demo rehearsal checklist |

---

## 15. Technical limitations and risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **Data availability**: most Karachi lost/found posts are in Facebook groups, which have no lawful API access. X costs money. Reddit needs approval. RSS volume is low. | **High** | Be transparent in the report. Build the connector framework well. Budget-capped X. Apply for Reddit early. Consider the community-submission connector. Demo mode uses labelled synthetic data and is never presented as collected. |
| 2 | **Hardware**: 2-core CPU, 8 GB RAM, no GPU. AI is slow (estimated 20–60 s/post). Running Docker + Ollama + Next dev + VS Code + a browser together may cause swapping. | **High** | Cap WSL at 2 GB (`.wslconfig`). One model loaded at a time. Small `num_ctx`. Process the queue *before* demos. Use `next build && next start` for demos. Native PostgreSQL install is an option (see question 3). |
| 3 | **Small-model accuracy**, especially Roman Urdu and Urdu script | Medium | Few-shot prompts, deterministic grounding layer, `UNVERIFIED` status, honest measured metrics, configurable model |
| 4 | **Python 3.14** wheel availability for geo/DB libraries | Medium | `uv` manages a project-local Python 3.12 without touching your system Python |
| 5 | **Karachi geocoding gaps**: OSM coverage of "Block 10A"-level areas is uneven and spellings vary | Medium | Alias table + OSM-derived gazetteer. Neighbourhood-level circles instead of fake precision. Unmapped reports stay in the list. |
| 6 | **Privacy**: posts contain CNIC numbers, phone numbers, names | Medium | Redaction, no author storage, purge irrelevant text, link to the original instead of copying contact details |
| 7 | **Platform terms**: X Developer Policy (deletion compliance, display rules), OSM tile and Nominatim usage policies | Medium | Compliance notes per connector. Nominatim at 1 req/s with caching. OSM tiles are fine for dev/demo traffic; a tile provider for public deployment. |
| 8 | **Anonymous suggestion abuse** | Low–Med | Suggestions never change status. Fingerprint hashing, rate limits, honeypot. |
| 9 | **Demo-day internet failure** (tiles, Nominatim) | Medium | Pre-geocoded data in the DB. Rehearse offline. Optionally cache tiles for the Karachi area at demo zooms (within tile policy). |
| 10 | **External API pricing/policies change** (X changed its model in 2025–26) | Medium | Everything credential-gated, configurable, and documented with a "verified on" date |

---

## 16. What to install on your Windows computer

**Required**

1. **Ollama for Windows**: download the installer from <https://ollama.com/download>. Then:
   ```powershell
   ollama --version
   ollama pull qwen2.5:3b        # ~2 GB; other benchmark models pulled in Phase 3
   ```
2. **Docker Desktop**: already installed. Start it, and before that create `C:\Users\Administrator\.wslconfig` to protect your RAM:
   ```ini
   [wsl2]
   memory=2GB
   processors=2
   swap=2GB
   ```
   then run `wsl --shutdown` and restart Docker Desktop.
3. **uv** (Python project and version manager, free, open source):
   ```powershell
   winget install --id=astral-sh.uv -e
   uv python install 3.12
   ```
4. **VS Code extensions**: Python, Pylance, Ruff, ESLint, Prettier, Tailwind CSS IntelliSense.

**Already present**: Node.js 24 (npm), Git, Docker Desktop.

**Installed by the project later (no action needed now)**: npm packages, Python packages via `uv sync`, Playwright browsers (`npx playwright install chromium`, ~150 MB) in Phase 6.

**Optional**: DBeaver or pgAdmin (DB browsing). X developer account with prepaid credits (only if you choose to fund it). Reddit API access application. Bluesky account app password.

---

## 17. Assumptions made

1. Initial geography is **Karachi only**; the data model is multi-city.
2. UI language is English; ingest supports English, Roman Urdu, and Urdu.
3. A report is a single lost or found item. Missing persons and pets are allowed as categories but get no special workflow.
4. Reports are public listings that link back to the original post. We do not host contact details.
5. "Admin" functions are limited to CLI maintenance commands for the person running the system.
6. GitHub remote, CI, and deployment targets are decided in Phase 6.

## 18. Decisions needed from you before Phase 1

1. **X API budget**: are you willing to prepay a small amount of X credits (e.g. $5–10) for the demo, or should X stay a fixture-tested, documented-but-inactive connector?
2. **Community submission connector**: may we add a "Submit a public post" form (text + public URL, rate-limited, labelled as community-submitted, processed by the same AI pipeline)? This is the most practical lawful way to get Karachi-relevant data, but it moves slightly beyond pure aggregation.
3. **PostgreSQL hosting**: Docker (as specified; costs ~1–2 GB RAM for the WSL VM) **or** a native Windows PostgreSQL + PostGIS install (lighter on 8 GB RAM, slightly more manual setup)?
4. **Resolution verification**: is an operator **CLI command** to accept or reject resolution suggestions acceptable (it is not a dashboard and has no accounts)? Otherwise reports become `RESOLVED` only from source evidence or `STALE` by age.
5. **Reddit**: should I prepare the text for a Reddit Data API access request (academic, non-commercial) so you can submit it early?
