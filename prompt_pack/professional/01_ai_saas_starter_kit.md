# 🚀 AI SaaS Starter Kit

**Difficulty:** Professional

## Overview
A production-ready boilerplate for building and shipping AI-powered SaaS products. It handles authentication, billing, API key management, rate limiting, usage tracking, and deployment — so you can focus on your product logic, not infrastructure.

## Objectives
- A FastAPI backend with clean modular architecture
- User authentication with JWT + refresh tokens
- Stripe integration for subscription billing (free tier, pro tier)
- API key management per user (create, revoke, rotate)
- Rate limiting and usage tracking per API key
- Structured logging and error tracking
- Docker-based deployment with environment configuration
- OpenAPI docs auto-generated from code
- Health checks and basic monitoring endpoints

## Features
- [ ] User signup/login/register with email verification
- [ ] JWT access + refresh token rotation
- [ ] Password reset flow
- [ ] Stripe webhook handler for subscription events
- [ ] API key CRUD per user (generate, view, revoke, rotate)
- [ ] Per-key rate limiting (requests per minute)
- [ ] Usage tracking: requests made, tokens consumed, cost estimated
- [ ] Usage dashboard endpoint for per-user analytics
- [ ] Structured JSON logging with request correlation IDs
- [ ] Centralised error handling with consistent error responses
- [ ] OpenAPI/Swagger docs at /docs
- [ ] Docker Compose for local development
- [ ] Health check endpoint at /healthz
- [ ] Database migrations via Alembic
- [ ] Unit tests for auth, billing, and API key logic
- [ ] README with setup, deployment, and contribution guide

## Technical Suggestions
- **Python + FastAPI** — async, performant, great OpenAPI support
- **SQLAlchemy + Alembic** — ORM and migrations
- **PostgreSQL** — production database (SQLite for dev)
- **Stripe** — payment processing (use test keys in dev)
- **Redis** — rate limiting and caching
- **structlog** — structured JSON logging
- **Pydantic** — request/response validation
- **Docker + Docker Compose** — reproducible environments
- **pytest + httpx** — testing framework and async HTTP client
- **Jinja2 or HTMX** — for any admin dashboard views

## Stretch Goals
- Add a usage-based billing model (pay per token/request)
- Implement webhook delivery with retry logic for external integrations
- Add an admin dashboard to view all users, keys, and usage
- Implement API key scopes (read-only, full access, custom)
- Add SSO support (Google, GitHub)
- Build a simple React/Vue frontend for the user portal
- Add Prometheus metrics endpoint for monitoring

## Learning Outcomes
You'll learn how to architect a multi-tenant SaaS backend, handle payment integrations responsibly, implement proper API key lifecycle management, and ship something that could actually generate revenue. This teaches production thinking: logging, monitoring, rate limiting, security, and deployment.

## AI Instructions
1. Analyse the repository structure before writing any code. Check for existing config, requirements, or setup files.
2. Create a detailed implementation plan: database schema, API routes, auth flow, billing integration order.
3. Ask clarifying questions if requirements are ambiguous (Stripe pricing tiers, rate limit defaults, auth strategy).
4. Work iteratively — start with user auth, then API keys, then billing, then monitoring.
5. Explain major architectural decisions (why SQLAlchemy over raw SQL, why Redis for rate limiting, JWT vs session).
6. Keep milestones logically separated: auth → API keys → billing → usage tracking → logging → deployment → polish.
7. Avoid unnecessary complexity — start with email/password auth, add SSO later if needed.
8. Write clear documentation: README with architecture diagram, setup steps, environment variables, and deployment guide.
9. Add tests for auth flows, API key generation/revocation, rate limiting logic, and webhook handling.
10. Refactor when improvements become obvious — keep modules small and focused.
11. Pause after completing each major feature and summarise progress with what was built and what's next.
