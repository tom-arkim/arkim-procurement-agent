# Arkim Sourcing Engine — Frontend

React/Next.js frontend for the Arkim production platform. Requires the
FastAPI backend (`api_server.py`) running alongside it during development.

---

## Prerequisites

| Tool | Version |
|------|---------|
| Node.js | 18+ (22 recommended) |
| npm | 10+ |
| Python | 3.10+ |
| pip | latest |

---

## Running locally

### 1 · Install Python dependencies (backend)

From the **repository root** (`Arkim Procurement Agent Prototype/`):

```powershell
# Activate the project venv first (PowerShell)
.\venv_win\Scripts\Activate.ps1
pip install -r requirements.txt
```

`fastapi`, `uvicorn[standard]`, and `python-multipart` are already in
`requirements.txt`. If you prefer a clean install without the venv:

```powershell
pip install fastapi "uvicorn[standard]" python-multipart sqlalchemy
```

### 2 · Start the FastAPI backend

```powershell
# From the repository root (venv active)
uvicorn api_server:app --reload --port 8000
```

The API is available at `http://localhost:8000`.  
Interactive docs: `http://localhost:8000/docs`

### 3 · Configure environment

A `.env.local` file is already committed at `frontend/.env.local`:

```
NEXT_PUBLIC_API_URL=http://localhost:8000
```

This tells the frontend where the FastAPI backend lives. Update the value
if your backend runs on a different host or port.

### 4 · Install frontend dependencies

```bash
cd frontend
npm install
```

### 5 · Start the Next.js dev server

```bash
# From frontend/
npm run dev
```

The app is available at `http://localhost:3000`.

All `/api/*` requests are proxied to `localhost:8001` via the Next.js rewrite
configured in `next.config.ts` — no CORS headers or separate fetch base URL
needed in the frontend code.

---

## Running both services in one terminal (Windows PowerShell)

```powershell
# Tab 1 — backend (venv active)
uvicorn api_server:app --reload --port 8000

# Tab 2 — frontend
cd frontend; npm run dev
```

Or use VS Code's compound launch configuration (add `.vscode/launch.json`):

```json
{
  "version": "0.2.0",
  "compounds": [{
    "name": "Arkim full stack",
    "configurations": ["FastAPI", "Next.js"]
  }],
  "configurations": [
    {
      "name": "FastAPI",
      "type": "python",
      "request": "launch",
      "module": "uvicorn",
      "args": ["api_server:app", "--reload", "--port", "8000"],
      "cwd": "${workspaceFolder}"
    },
    {
      "name": "Next.js",
      "type": "node",
      "request": "launch",
      "runtimeExecutable": "npm",
      "runtimeArgs": ["run", "dev"],
      "cwd": "${workspaceFolder}/frontend"
    }
  ]
}
```

---

## Verify Phase 1 is working

1. Backend health: `GET http://localhost:8000/api/health` → `{ "status": "ok" }`
2. Frontend: `http://localhost:3000` → redirects to `/runs` (placeholder screen)
3. Frontend → backend proxy: click **API HEALTH CHECK** link on the runs page

---

## Project structure

```
frontend/
├── src/
│   ├── app/                  # Next.js App Router
│   │   ├── layout.tsx        # Root layout (fonts, providers)
│   │   ├── globals.css       # CSS custom properties + Tailwind base
│   │   ├── providers.tsx     # TanStack Query provider
│   │   ├── page.tsx          # Root redirect → /runs
│   │   └── runs/
│   │       ├── page.tsx      # Run list (Phase 2)
│   │       └── [id]/
│   │           └── page.tsx  # Run detail (Phase 2)
│   ├── components/
│   │   ├── ui/               # shadcn/ui base components
│   │   └── arkim/            # Domain-specific components (Phase 2)
│   ├── lib/
│   │   ├── api.ts            # Typed fetch wrappers for every endpoint
│   │   ├── queries.ts        # TanStack Query hooks
│   │   └── query-client.ts   # QueryClient singleton + key factory
│   ├── store/
│   │   └── index.ts          # Zustand client state (UI, drafts, toasts)
│   └── types/
│       └── index.ts          # TypeScript types mirroring data-model.md
├── tailwind.config.ts        # Design tokens → Tailwind theme
├── next.config.ts            # Rewrite proxy /api/* → :8001
├── tsconfig.json
├── package.json
└── README.md
```

---

## Design system reference

The visual specification lives in the `/design/` directory at the repo root:

| File | Purpose |
|------|---------|
| `Arkim Sourcing Engine.html` | Interactive design canvas (open in browser) |
| `styles.css` | CSS custom properties (ported to `globals.css` + `tailwind.config.ts`) |
| `data-model.md` | Canonical type definitions (mirrored in `src/types/index.ts`) |
| `interactions.md` | Behavioral spec for every action |

---

## Tech stack

| Layer | Library |
|-------|---------|
| Framework | Next.js 14 (App Router) |
| Language | TypeScript (strict) |
| Styling | Tailwind CSS + CSS custom properties |
| Component base | shadcn/ui (customized) |
| Server state | TanStack Query v5 |
| Client state | Zustand v4 with Immer |
| Icons | Lucide React |
| Fonts | Inter + JetBrains Mono (via `next/font`) |

---

## Phase roadmap

| Phase | Scope |
|-------|-------|
| **1 (current)** | Infrastructure: Next.js scaffold, design tokens, API layer, types |
| 2 | Intake screen: chat UI, nameplate upload, spec extraction panel |
| 3 | Sourcing dashboard: three-tier layout, vendor cards, spec comparison |
| 4 | Approval workflow: single/dual approver, rejection notes |
| 5 | Tier 3 outreach flow: drafts, send, tracking timeline |
| 6 | Run history, admin rules, mobile companion |
