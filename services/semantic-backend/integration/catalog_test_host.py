"""Loopback model socket fixture plus the actual backend; never a production fallback."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import uvicorn


async def serve(fixtures: Path, backend_port: int, model_port: int, host: str) -> None:
    if os.environ.get("SEMANTIC_NEXUS_ENVIRONMENT") != "Development":
        raise RuntimeError("The catalog socket fixture is development-only")
    candidates = json.loads((fixtures / "catalog-model.json").read_text(encoding="utf-8"))
    active = set()
    target = fixtures / "catalog-observations" / "model.json"
    sequence = json.loads(target.read_text(encoding="utf-8"))["sequence"] if target.exists() else 0
    if type(sequence) is not int or sequence < 0:
        raise ValueError("Invalid synthetic observation sequence")

    def record(state: str) -> None:
        nonlocal sequence
        sequence += 1
        temporary = fixtures / "catalog-observations" / "model.tmp"
        temporary.write_text(json.dumps({"sequence": sequence, "state": state}), encoding="utf-8")
        temporary.replace(target)

    async def handle(reader, writer):
        task = asyncio.current_task()
        active.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.split(": ", 1) for line in head.decode().split("\r\n")[1:] if line)
            size = int(headers["Content-Length"])
            if not 0 < size <= 262144:
                raise ValueError("Oversized synthetic request")
            payload = json.loads(await reader.readexactly(size))
            context = json.loads(payload["messages"][1]["content"])["context"]
            if context["question"].endswith("[synthetic-disconnect]"):
                record("waiting")
                await reader.read()
                record("cancelled")
                return
            candidate = candidates[context["catalog"]["content_sha256"]]
            body = json.dumps(
                {
                    "id": "synthetic-loopback",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "synthetic-public-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(candidate)},
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
                }
            ).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n")
            writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            active.discard(task)

    model = await asyncio.start_server(handle, "127.0.0.1", model_port)
    try:
        async with model:
            config = uvicorn.Config(
                "semantic_backend.api:create_app",
                factory=True,
                host=host,
                port=backend_port,
                log_level="warning",
            )
            await uvicorn.Server(config).serve()
    finally:
        for task in tuple(active):
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--backend-port", type=int, default=8080)
    parser.add_argument("--model-port", type=int, default=8099)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    args = parser.parse_args()
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(serve(args.fixtures, args.backend_port, args.model_port, args.host))
