from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body)
            print(json.dumps({
                "event": "grafana_alert",
                "title": parsed.get("title", ""),
                "state": parsed.get("state", ""),
                "alert_count": len(parsed.get("alerts", [])),
            }), flush=True)
        except Exception:
            print(json.dumps({"event": "grafana_alert", "raw": body.decode()}), flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass

HTTPServer(('0.0.0.0', 9999), Handler).serve_forever()
