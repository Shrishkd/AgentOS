"""A credential-free Chat Completions peer used only in the disposable test network."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from time import sleep
from uuid import uuid4

seen = set()
lock = Lock()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path != "/v1/chat/completions" or payload["model"] != "fixture-model":
            self.send_error(400)
            return
        messages = payload["messages"]
        assert messages[0]["role"] == "system" and payload["max_completion_tokens"] > 0
        data = json.loads(messages[1]["content"])
        mode = data["mode"]
        if mode == "retry":
            with lock:
                first = data["key"] not in seen
                seen.add(data["key"])
            if first:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
        if mode == "timeout":
            sleep(2)
        if mode == "tool" and messages[-1]["role"] == "user":
            assert payload["tools"][0]["function"]["name"] == "text_length"
            message = {"tool_calls": [{"id": "call_1", "type": "function",
                       "function": {"name": "text_length", "arguments": '{"text":"hello"}'}}]}
            reason = "tool_calls"
        elif mode == "tool":
            assert messages[-1]["tool_call_id"] == "call_1" and json.loads(messages[-1]["content"]) == {"length": 5}
            message, reason = {"content": "five"}, "stop"
        else:
            message, reason = {"content": "ok"}, "stop"
        result = {"choices": [{"message": message, "finish_reason": reason}],
                  "usage": {"prompt_tokens": 12, "completion_tokens": 4}}
        self.send_response(200)
        self.send_header("x-request-id", str(uuid4()))
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
