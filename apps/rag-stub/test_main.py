import json
import re

from fastapi.testclient import TestClient

import main

main.STREAM_DELAY = 0
client = TestClient(main.app)
URL = "/v1/chat/completions"
LINK = re.compile(r"/source/([A-Z0-9-]+)#s-([0-9.]+)")


def ask(text, **extra):
    return client.post(URL, json={"model": "rag-demo", "messages": [{"role": "user", "content": text}], **extra})


def content(resp):
    return resp.json()["choices"][0]["message"]["content"]


def test_e214_answer_has_numbered_citations_that_resolve():
    answer = content(ask("SN 20-4471 steht mit E-214. Was tun?"))
    for n in range(1, 6):
        assert f"[[{n}]](" in answer
    assert "/source/BA-M200-DE#s-7.3.1" in answer
    for doc, sec in set(LINK.findall(answer)):
        page = client.get(f"/source/{doc}")
        assert page.status_code == 200
        assert f'id="s-{sec}"' in page.text


def test_routing_uses_last_user_message_and_content_parts():
    msgs = [
        {"role": "user", "content": "E-214?"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": [{"type": "text", "text": "Wie wird das Wetter?"}]},
    ]
    r = client.post(URL, json={"model": "rag-demo", "messages": msgs, "temperature": 0.3})
    assert content(r).startswith("UNZUREICHENDE BELEGE:")
    assert "/source/" not in content(r)
    msgs[-1]["content"] = [{"type": "text", "text": "und e-214?"}]
    assert "/source/" in content(client.post(URL, json={"messages": msgs}))


def test_stream_is_openai_sse_and_matches_non_stream():
    r = ask("E-214?", stream=True)
    assert r.headers["content-type"].startswith("text/event-stream")
    lines = [l for l in r.text.split("\n\n") if l]
    assert all(l.startswith("data: ") for l in lines)
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(l[6:]) for l in lines[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    streamed = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert streamed == content(ask("E-214?"))


def test_bad_input_and_unknown_source():
    assert client.post(URL, content="nope", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post(URL, json={"messages": "x"}).status_code == 400
    assert client.get("/source/NOPE").status_code == 404
    assert client.get("/healthz").json() == {"status": "ok"}
