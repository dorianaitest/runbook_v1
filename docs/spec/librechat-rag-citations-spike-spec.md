# Spec: LibreChat RAG citations spike

**Goal:** In LibreChat on the VPS, a test user can ask the dummy "RAG-Demo" endpoint about E-214, gets a streamed German answer with numbered citations, and clicking a citation opens that source page in a new tab with the cited section highlighted.

**Date:** 2026-11-08

**Status:** draft

**Branch:** `feat/rag-stub-citations-spike`

---

## Background

The target UX is the demo on https://solasas-tech-rag.netlify.app/ (section "Demo").
There, each citation marker and each "Quellen" chip opens a side panel showing the cited document page with the cited section highlighted.
LibreChat cannot show such a panel for an external backend without a frontend fork.
This spike tests the cheapest close alternative: the backend is an OpenAI-compatible custom endpoint, citations are Markdown links, and each link opens a small source viewer page in a new tab.
The real RAG is not ready, so a stub with canned answers stands in for it.
Once the spike passes, the real RAG only has to produce the same answer format.

## Components Involved

- **`apps/rag-stub/`** (new) - FastAPI app: `/v1/chat/completions` (stream and non-stream) with canned answers, `/source/<id>` viewer pages, `/healthz`, and a request log with structure only (roles, message count, set fields), never message content.
- **`apps/rag-stub/docker-compose.yml`** (new) - container on `app_net` (LibreChat reaches it at `http://rag-stub:8000`) and `proxy` (Traefik route for the viewer pages).
- **`overrides/librechat/librechat.yaml`** - one new entry under `endpoints.custom`: `RAG-Demo`.
- **`apps/rag-stub/README.md`** (new) - manual checklist that the user runs in the browser, because LibreChat is behind a VPN that does not work together with Claude Code.
- **Traefik** - picks up the stub via container labels only; no changes to `infra/traefik/`.

## Dummy content

All documents are fictional and in German, modeled on the demo.

| ID | Type | Location | Content |
| --- | --- | --- | --- |
| `FL-M200` | Fehlerliste | S. 9, E-214 | E-214: Anpressdruck der Quersiegelung außerhalb des Sollbereichs, Maschine stoppt im nächsten Takt |
| `BA-M200-DE` | Betriebsanleitung | S. 47, 7.3.1 to 7.3.6 | Wartung der Quersiegelstation; 7.3.1 spannungsfrei schalten, 7.3.5 Sollwert 4,2 bar ± 0,2 bar, E-214 ab Revision B |
| `SB-2025-0412` | Servicebericht | S. 1, Ursache | Druck nach Backenwechsel nicht neu eingestellt (3,7 bar), undichte Schlauchverschraubung |
| `AR-M200` | Anlagenregister | S. 1, SN 20-4471 | SN 20-4471 ist M-200 HS, Revision B |

Routing in the stub:

- The last user message contains `E-214` -> the cited answer, closely following the demo answer (bold heading, "Maßnahmen" bullets, "Ähnlicher Servicefall", a "Quellen" list).
- Anything else -> a canned abstention starting with `UNZUREICHENDE BELEGE:`, without citations.

Citation format in the answer (Markdown, absolute URLs):

```markdown
Vorher die Station spannungsfrei schalten und sichern [[2]](https://rag-stub.demo-ki-instanz.de/source/BA-M200-DE#s-7.3.1).
```

The answer ends with a "Quellen" list where each entry is a link, e.g. `2. [BA-M200-DE, S. 47, 7.3.1](https://rag-stub.demo-ki-instanz.de/source/BA-M200-DE#s-7.3.1)`.
The public base URL comes from an environment variable (`PUBLIC_BASE_URL`), so a local run links to `http://localhost:8000`.
The viewer page highlights the section named in the URL fragment (`:target` CSS plus `scroll-margin`), so one page per document serves every cited section.

## Assumptions

- `runbook_v1` deploys to the VPS behind `chat.demo-ki-instanz.de`, via commit, push and `git pull` in `/srv/runbook_v1` (`/srv/apps` and `/srv/overrides` are symlinks into it).
- The VPN that protects LibreChat also covers `rag-stub.demo-ki-instanz.de`, so the viewer pages are reachable wherever the chat is.
- The `letsencrypt-dns` cert resolver can issue a certificate for `rag-stub.demo-ki-instanz.de`.
- The viewer is a plain HTML page; mimicking the demo's panel styling is out of scope.
- It is acceptable that every LibreChat user briefly sees the `RAG-Demo` endpoint, and that LibreChat restarts once to load the yaml change.
- The user runs the Step 5 check by hand with their own LibreChat account.

## Risks

- LibreChat strips or rewrites the citation links -> this is what the spike tests; fallback is a plain "Quellen" list with full URLs.
- LibreChat sends a different request shape than expected (title call, model list, full history, extra params) -> the stub accepts and ignores unknown fields, and the request log records what arrives.
- A broken `librechat.yaml` keeps LibreChat from starting -> parse the yaml locally before committing; rollback is `git revert` plus a pull.
- New public subdomain -> it serves only fictional pages, logs no content, and is removed in Step 6 unless kept on purpose.

## Out of Scope

- Real retrieval, embeddings or any LLM call.
- LibreChat source or frontend changes, including a side panel.
- Changes to `fastapi-gateway`, LiteLLM, the PII gate or `infra/traefik/`.
- Auth on the stub and production hardening.
- Pixel-matching the demo.

## Implementation Steps

### Step 1 - Stub server, runnable locally

What to do: write `apps/rag-stub/main.py` (FastAPI, no other dependencies) with the routing, canned answers, viewer pages, request log and `/healthz`, plus `apps/rag-stub/requirements.txt` and a small `apps/rag-stub/test_main.py` covering routing, citation links and the SSE format.

Acceptance check:
```bash
cd apps/rag-stub
uv run --with fastapi --with uvicorn --with httpx --with pytest pytest -q
uv run --with fastapi --with uvicorn uvicorn main:app --port 8000 &
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"rag-demo","messages":[{"role":"user","content":"SN 20-4471 steht mit E-214. Was tun?"}]}' | rg -o '/source/[A-Z0-9-]+#s-[0-9.]+' | sort -u
curl -sN localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"rag-demo","stream":true,"messages":[{"role":"user","content":"E-214?"}]}' | tail -2   # last line: data: [DONE]
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"rag-demo","messages":[{"role":"user","content":"Wie wird das Wetter?"}]}' | rg -c 'UNZUREICHENDE BELEGE'
curl -s localhost:8000/source/BA-M200-DE | rg -c 'id="s-7.3.1"'
```

### Step 2 - Container and Traefik route

What to do: `apps/rag-stub/Dockerfile` (python:3.11-slim, uvicorn on port 8000) and `apps/rag-stub/docker-compose.yml` modeled on `apps/pii-gate/docker-compose.yml` plus the Traefik labels pattern from `apps/fastapi/docker-compose.yaml`: networks `app_net` and `proxy` (both external), router `Host(\`rag-stub.demo-ki-instanz.de\`)`, `websecure`, `letsencrypt-dns`, `frame-deny@file`, healthcheck on `/healthz`, `PUBLIC_BASE_URL=https://rag-stub.demo-ki-instanz.de`.

Acceptance check (compose syntax locally if Docker is available, otherwise on the VPS):
```bash
docker compose -f apps/rag-stub/docker-compose.yml config -q
```

### Step 3 - LibreChat endpoint

What to do: add under `endpoints.custom` in `overrides/librechat/librechat.yaml`:

```yaml
    - name: 'RAG-Demo'
      apiKey: 'dummy'
      baseURL: 'http://rag-stub:8000/v1'
      models:
        default:
          - 'rag-demo'
        fetch: false
      titleConvo: false
      modelDisplayLabel: 'RAG-Demo'
```

Check whether `rag-stub` must be added to `allowedAddresses` (the yaml's SSRF exemption list, see the comments near line 295) for an admin-defined `baseURL`; the existing `fastapi-gateway` endpoints are the reference.

Result: not needed. The SSRF check only applies to user-provided baseURLs, and the `fastapi-gateway` endpoints work without an entry.

Because `interface.modelSelect` is `false`, the picker shows only `modelSpecs` entries, so the endpoint also needs a spec (added after the first deploy, when RAG-Demo did not show up):

```yaml
    - name: "rag-demo"
      label: "RAG-Demo"
      group: "RAG-Demo"
      hideBadgeRow: true
      preset:
        endpoint: "RAG-Demo"
        endpointType: "custom"
        model: "rag-demo"
```

It deliberately omits `<<: *specDefaults`, which would turn on web search, code execution, file search and artifacts and add tools to the requests the spike observes.

Acceptance check:
```bash
uv run --with pyyaml python -c "import yaml; c=yaml.safe_load(open('overrides/librechat/librechat.yaml')); print([e['name'] for e in c['endpoints']['custom']])"
```

### Step 4 - Deploy (user, on the VPS)

What to do: commit Steps 1 to 3 on the branch and push; the user pulls and deploys.

Before the first deploy, the user adds a DNS `A` record `rag-stub` (same IP as `chat`) in the Hetzner Console zone `demo-ki-instanz.de`.

Acceptance check (user, on the VPS):
```bash
# /srv itself is not a git checkout; /srv/apps and /srv/overrides are symlinks into /srv/runbook_v1.
cd /srv/runbook_v1 && git fetch && git switch feat/rag-stub-citations-spike && git pull
docker compose -f apps/rag-stub/docker-compose.yml config -q && echo "compose ok"
docker compose -f apps/rag-stub/docker-compose.yml up -d --build
curl -s https://rag-stub.demo-ki-instanz.de/healthz
# Recreate (not restart) the api container: git pull replaces librechat.yaml with a new
# inode, and a single-file bind mount keeps the old one until the container is recreated.
cd /srv/apps/librechat && bash -e -c '
  set -a
  source <(sed "/^[[:space:]]*UID=/d" .env)
  source /srv/runbook_v1/load-secrets.sh
  set +a
  docker compose --env-file .env \
    -f docker-compose.yml \
    -f /srv/overrides/librechat/docker-compose.override.yml \
    up -d --force-recreate api
'
docker logs LibreChat 2>&1 | tail -50 | grep -iE "error|invalid|memory" || echo "no config errors"
```

The wrapper is the one the VPS already uses for LibreChat (taken from the shell history), and `rg` is not installed on the VPS, so the commands use `grep`.

### Step 5 - Manual check (user, in the browser on the VPN)

What to do: the user runs the manual checklist in `apps/rag-stub/README.md` in LibreChat (an automated Playwright script was dropped in favor of the manual check).
In short: a new `RAG-Demo` chat, the E-214 question with citations `[1]` to `[5]`, a click on `[2]` that opens `/source/BA-M200-DE#s-7.3.1` highlighted in a new tab, an off-topic question that abstains, and a follow-up in the same chat.

Acceptance check: every checklist item passes, plus the request log on the VPS:
```bash
docker logs rag-stub 2>&1 | grep '"event": "request"'
```

### Step 6 - Results and cleanup

What to do: record per-check pass/fail, the screenshots and the request-log findings (is full history sent, are title or model-list calls made, is `stream` always true) in a "Results" section of this spec.
Then decide with the user: keep the stub for the real RAG, or remove the endpoint and stop the container.
Removal covers the `RAG-Demo` endpoint and the `rag-demo` model spec in `librechat.yaml`, the container, and the `rag-stub` DNS record.

Acceptance check (cleanup case, user, on the VPS):
```bash
docker compose -f apps/rag-stub/docker-compose.yml down
curl -s -o /dev/null -w '%{http_code}\n' https://rag-stub.demo-ki-instanz.de/healthz   # not 200
```

## Checkpoints

- After Step 1: the user reviews the answer text and how the citations look in the curl output before anything is committed for deployment.
- After Step 3: the user reviews the diff, then Claude commits and pushes the branch.
- After Step 4: the user confirms the stub is live and LibreChat restarted cleanly.
- After Step 5: the user reports the checklist results and pastes the request log; together we decide how the real integration handles history and titles.

## Results

**Date:** 2026-11-09

**Outcome:** the spike passed.
LibreChat keeps Markdown citation links intact, so the real RAG only has to produce the same answer format.

### Manual check (Step 5)

All checklist items passed (user, in the browser on the VPN):

- The answer streams, with heading, "Maßnahmen", "Ähnlicher Servicefall" and "Quellen".
- Markers `[1]` to `[5]` and all Quellen entries are clickable links.
- Clicking `[2]` opens `/source/BA-M200-DE#s-7.3.1` in a new tab, with 7.3.1 highlighted.
- The off-topic question abstains with `UNZUREICHENDE BELEGE:` and no links.
- The follow-up in the same chat gets the cited answer again.
- After a reload, the links in the saved chat still work.

Screenshots were taken by the user and are not in the repo.

### Request log findings

Six requests over two chats (one earlier manual test, one checklist run):

| # | stream | message_count | roles |
| --- | --- | --- | --- |
| 1 | false | 1 | user |
| 2 | true | 2 | system, user |
| 3 | false | 1 | user |
| 4 | true | 2 | system, user |
| 5 | true | 4 | system, user, assistant, user |
| 6 | true | 6 | system, user, assistant, user, assistant, user |

Every request had exactly the fields `messages`, `model`, `stream`, `user`.

- **Full history is sent on every turn** (2, 4, 6 messages). The real RAG can stay stateless; it should retrieve on the last user message and may use the history to resolve follow-ups like "und bei E-214?".
- **Chat turns always stream** (`stream: true`).
- **One extra non-streamed call per new chat**, with a single user message, only before the first turn and never on follow-ups. This pattern matches LibreChat's title generation, even though `titleConvo: false` is set; not confirmed, because the stub does not log content. Check: if the sidebar title of a RAG-Demo chat looks like the start of the canned answer, it is the title call. The real RAG must answer it cheaply (or the call must be stopped), so it does not run a full retrieval per new chat.
- **LibreChat always prepends a system message** on chat turns, although the `rag-demo` spec sets no `promptPrefix`. Its content is unknown (likely LibreChat's own instructions or injected memory). The real RAG should accept it and must not rely on being the only one that sets the system prompt.
- **The `user` field is set** on every request (LibreChat's user id). The real RAG can use it for per-user audit logging; it is a pseudonymous identifier, so it falls under the DSGVO logging rules.
- **No model-list calls and no extra parameters** (no `temperature`, `max_tokens`, `tools`), as expected with `fetch: false` and no `specDefaults`.

### Open items

- Confirm the non-streamed call is the title call (sidebar title check), and decide whether the real RAG handles or blocks it.
- Memory agent on `gemini-2.5-flash`: confirm in Langfuse or `docker logs LibreChat` that memory calls work.
- Keep or remove the stub, then switch `/srv/runbook_v1` back to `main` or merge the branch.
