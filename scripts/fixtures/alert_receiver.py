"""Private deployment-drill webhook. Log only alert names from received batches."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        batch = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        for alert in batch["alerts"]:
            print(alert["labels"]["alertname"], flush=True)
        self.send_response(200)
        self.end_headers()


HTTPServer(("", 9099), Receiver).serve_forever()
