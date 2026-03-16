# IP6 — A2A Protocol Demo

Exploring the [A2A (Agent-to-Agent) protocol](https://a2a-protocol.org/) with Pydantic AI and the official a2a-sdk at FHNW.

## Setup

1. Copy `.env.example` to `.env` and add your OpenRouter API key:
   ```bash
   cp .env.example .env
   ```

2. Install dependencies:
   ```bash
   uv sync
   ```

## Running

Open two terminals:

**Terminal 1 — Start Agent A (Server):**
```bash
uv run uvicorn agent_a_server:app --port 8000
```

**Terminal 2 — Run Agent B (Client):**
```bash
uv run python agent_b_client.py
```

Agent B connects to Agent A, requests a joke, and translates it to German.
