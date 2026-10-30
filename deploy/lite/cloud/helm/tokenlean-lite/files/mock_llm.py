"""A stand-in OpenAI-compatible chat endpoint for the LITE smoke test (compose profile `mock`).

Standard library only, so it runs on the proxy image with nothing installed. It answers
POST /v1/chat/completions with a fixed reply, as a JSON completion or as an SSE stream that
ends with a usage chunk when the caller asks for one (the proxy always does), and reports
token usage so the proxy has something to price. It never calls anything.
"""
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY = "pong from the LITE mock"


def _usage(body):
    prompt = max(1, len(json.dumps(body.get("messages", []))) // 4)
    completion = max(1, len(REPLY) // 4)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # one line per request, to stdout
        print("mock-llm:", fmt % args, flush=True)

    def _json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/health", "/v1/models"):
            self._json(200, {"status": "ok", "data": [{"id": "lite-mock-1"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": {"message": "invalid JSON"}})
            return
        model = body.get("model", "lite-mock-1")
        cid = "chatcmpl-mock-" + uuid.uuid4().hex[:12]
        created = int(time.time())
        usage = _usage(body)
        if not body.get("stream"):
            self._json(200, {
                "id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY},
                             "finish_reason": "stop"}],
                "usage": usage,
            })
            return
        head = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
        frames = [
            {**head, "choices": [{"index": 0, "delta": {"role": "assistant", "content": REPLY},
                                  "finish_reason": None}]},
            {**head, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        if (body.get("stream_options") or {}).get("include_usage"):
            frames.append({**head, "choices": [], "usage": usage})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for frame in frames:
            self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    port = int(os.getenv("MOCK_LLM_PORT", "8080"))
    print(f"mock-llm: listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
