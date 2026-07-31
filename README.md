# NetGuard

Intelligent Automated Network Change Management & Self-Healing Platform

## Overview

NetGuard is an automated network change management platform designed to reduce configuration errors, improve deployment reliability, and provide automatic rollback in case of failures.

The platform manages the complete lifecycle of a network configuration change:

```
Change Request → AI Risk Analysis → Syntax Validation → Network Sanity Checks
→ Manager Approval → Snapshot → Deployment → Health Monitoring
→ Success  |  Automatic Rollback
```

See `docs/SRS.pdf` for the full Software Requirements Specification.

## Key Features

- Automated network configuration deployment
- AI-based risk analysis
- Configuration syntax validation
- Automatic backup and rollback
- Real-time health monitoring
- Secure approval workflow
- Complete audit logging

## Project Structure

```
netguard/
├── backend/              FastAPI backend (API, business logic, network automation)
│   ├── app/
│   │   ├── api/          Route handlers (change requests, devices, deployments, auth)
│   │   ├── core/         Config, security, database session
│   │   ├── models/       SQLAlchemy ORM models
│   │   ├── schemas/      Pydantic request/response schemas
│   │   └── services/     Business logic (risk engine, validation, rollback, snapshot)
│   ├── tests/
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/              React + TypeScript + Tailwind dashboard
│   ├── src/
│   │   ├── components/    Reusable UI (RiskBadge, ConfigDiff, StatusCard...)
│   │   ├── pages/         Dashboard, Change Requests, Devices, Audit Log
│   │   └── lib/           API client, types
│   └── Dockerfile
├── docker/
│   └── docker-compose.yml Full local stack (Postgres, Redis, backend, frontend)
├── docs/                   SRS and design docs
└── .env.example
```

## Quick Start (Docker)

```bash
cp .env.example .env
docker compose -f docker/docker-compose.yml up --build
```

- Backend API: http://localhost:8000/docs
- Frontend:    http://localhost:5173

## Quick Start (Local, without Docker)

**Backend**
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

**Frontend**
```bash
cd frontend
npm install
npm run dev
```

## Roadmap (matches SRS functional requirements)

- [x] Project scaffold, FR-1 (auth stub), core data models
- [x] FR-2 Device inventory CRUD
- [x] FR-3 Change request workflow + approvals
- [x] FR-4 Config diff engine
- [x] FR-5 / FR-6 Syntax validation + AI risk scoring
- [x] FR-7 Snapshot service (encrypted backups) + git-style version history per device
- [x] FR-8 Deployment engine (Netmiko / NAPALM)
- [x] FR-9 Health monitoring (real polling over a configurable window, not a single check)
- [x] FR-10 Self-healing rollback (automatic) + manual rollback to any prior snapshot
- [x] FR-11 Notifications (email / Slack / Teams)
- [x] FR-12 Immutable audit log

## Change Management & Rollback

- Every deployment automatically snapshots (and encrypts) the device's config immediately before applying a change.
- After deploying, NetGuard actually polls the health suite -- infrastructure, routing, services -- every
  `HEALTH_MONITOR_POLL_INTERVAL_SECONDS` for up to `HEALTH_MONITOR_WINDOW_SECONDS` (see `.env.example`), not a
  single check right after the push. The first failing round triggers automatic rollback immediately.
- `GET /devices/{id}/snapshots` lists a device's full config version history.
- `POST /devices/{id}/rollback` (Network Administrators) restores any prior snapshot on demand. It runs through
  the exact same Snapshot → Deploy → Health Monitor pipeline as a normal change, so a manual rollback gets the
  same safety net -- including automatic rollback if the restore itself fails its health checks.

## Tech Stack

Frontend: React, TypeScript, Tailwind CSS, Recharts
Backend: FastAPI, Python, Celery, Redis, SQLAlchemy
Network Automation: Netmiko, NAPALM, Paramiko
Database: PostgreSQL
Monitoring: Prometheus, Grafana

## System Requirements

- Python 3.11+
- Node.js 20+
- Docker & Docker Compose
- PostgreSQL
- Redis