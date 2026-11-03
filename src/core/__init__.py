"""Shared backend core modules and defense-in-depth infrastructure packages.

Submodules and subpackages:
- `config`: Environment loading, runtime environment detection, CORS origins, and typed settings.
- `context`: Neutral per-request `ContextVar` state (`RequestContext`) and `RequestCorrelationMiddleware`.
- `logger`: Consistent colored console and structured JSON helper logging.
- `auth`: Google Identity Platform / Firebase ID token verification and tenant UID validation.
- `app_factory`: Shared FastAPI application factory wiring middleware, probes, and routers.
- `storage`: Multi-tenant local `/tmp` isolation, GCS snapshot sync, distributed locks, and ChromaDB.
- `ai_security`: Cloud Model Armor + DLP screening (`armor`), Gemini safety (`safety`), and model/input ceilings (`guardrails`).
- `compliance`: Regional residency (`residency`), Cloud Logging (`audit`), GDPR DSAR (`dsar`), EU AI Act provenance (`provenance`), and compliance policy builder (`policy`).
- `observability`: Structured cloud logging (`logging`), hierarchical tracing (`tracing`), and AI performance telemetry (`telemetry`).
"""

