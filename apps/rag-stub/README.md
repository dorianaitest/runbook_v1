# rag-stub

Dummy RAG backend for the LibreChat citations spike.
Spec: `docs/spec/librechat-rag-citations-spike-spec.md`.

It answers like an OpenAI chat endpoint with canned German text.
A question containing `E-214` gets a cited answer, anything else gets an `UNZUREICHENDE BELEGE:` abstention.
Citations are Markdown links to `/source/<id>#s-<section>`, a viewer page that highlights the cited section.
All documents are fictional, and the request log records structure only, never message content.

## Local run

```bash
cd apps/rag-stub
uv run --with fastapi --with uvicorn --with httpx --with pytest pytest -q
uv run --with fastapi --with uvicorn uvicorn main:app --port 8000
```

`PUBLIC_BASE_URL` sets the base of the citation links (default `http://localhost:8000`).

## Deploy (VPS)

The only git checkout on the VPS is `/srv/runbook_v1`; `/srv/apps` and `/srv/overrides` are symlinks into it.

```bash
cd /srv/runbook_v1 && git pull
docker compose -f apps/rag-stub/docker-compose.yml up -d --build
curl -s https://rag-stub.demo-ki-instanz.de/healthz
docker logs rag-stub 2>&1 | grep '"event": "request"'
```

LibreChat picks up `librechat.yaml` changes only after its `api` container is recreated, see the spec's Step 4.

## E2E check (locally, on the VPN)

Needs Node and the Playwright Chromium in `~/.cache/ms-playwright`.

```bash
cd apps/rag-stub/e2e
npm install
LIBRECHAT_URL=https://chat.demo-ki-instanz.de \
LIBRECHAT_EMAIL='<test account>' LIBRECHAT_PASSWORD='<password>' \
CHROME_PATH=~/.cache/ms-playwright/chromium-1247/chrome-linux64/chrome \
node check.mjs; echo "exit $?"
```

Set `HEADLESS=0` to watch the browser.
Each check prints `PASS` or `FAIL`, screenshots land in `e2e/screenshots/`, and the exit code is 0 only if all checks passed.
Exit code 2 means the credentials are missing.

Credentials only go into environment variables, never into the repo.

## Manual fallback checklist

Use this if the script fails for reasons unrelated to the spike, such as the login flow or changed selectors.

1. Log in to LibreChat and start a new chat with **RAG-Demo** from the model picker.
2. Ask `SN 20-4471 steht mit E-214. Was bedeutet das und was ist zu tun?` and wait for streaming to finish.
3. Check that the answer has a bold heading, "Maßnahmen" bullets, "Ähnlicher Servicefall" and a "Quellen" list.
4. Check that the markers `[1]` to `[5]` and all five "Quellen" entries are clickable links.
5. Click `[2]`. A new tab should open at `https://rag-stub.demo-ki-instanz.de/source/BA-M200-DE#s-7.3.1`, with section 7.3.1 highlighted in yellow.
6. In the same chat, ask `Wie wird das Wetter morgen?` and check that the answer starts with `UNZUREICHENDE BELEGE:` and has no links.
7. Ask `Und was mache ich bei E-214 direkt nach einem Backenwechsel?` and check that it gets the cited answer again.
8. Take a screenshot after steps 2, 5, 6 and 7.

Then read what LibreChat sent, on the VPS:

```bash
docker logs rag-stub 2>&1 | grep '"event": "request"'
```
