"""Entry point:  python -m mcp_server [--transport stdio|http] [--host H] [--port P] [--path /mcp]

Environment: MCP_TRANSPORT (default stdio; the container sets http), MCP_HOST (0.0.0.0),
MCP_PORT (8100), MCP_PATH (/mcp), MCP_API_KEY (optional, protects HTTP), BACKEND_URL (or API_URL), API_KEY,
LOG_LEVEL (logs are shipped to BACKEND_URL/logs/batch).
"""
from __future__ import annotations

import argparse
import os

from mcp_server.logging_setup import get_logger, setup_logging


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mcp_server", description="Hybrid RAG MCP server")
    p.add_argument("--transport", choices=["stdio", "http"],
                   default=os.getenv("MCP_TRANSPORT", "stdio").lower())
    p.add_argument("--host", default=os.getenv("MCP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8100")))
    p.add_argument("--path", default=os.getenv("MCP_PATH", "/mcp"))
    args = p.parse_args(argv)

    # Logging first: in stdio mode everything must go to stderr, never stdout.
    setup_logging(args.transport)
    log = get_logger("mcp_server")

    from mcp_server.api_client import APIClient
    from mcp_server.server import create_http_app, create_server

    api = APIClient()
    server = create_server(api)
    setup_logging(args.transport)  # the SDK calls logging.basicConfig on construction; re-assert ours

    if args.transport == "stdio":
        log.info("mcp_starting", extra={"transport": "stdio", "api_url": api.base_url})
        server.run("stdio")
        return

    import uvicorn

    api_key = os.getenv("MCP_API_KEY") or None
    app = create_http_app(server, path=args.path, host=args.host, api_key=api_key)
    log.info("mcp_starting", extra={"transport": "http", "host": args.host, "port": args.port,
                                    "path": args.path, "api_url": api.base_url,
                                    "auth": bool(api_key)})
    # log_config=None: uvicorn's loggers propagate to our JSON root handler.
    uvicorn.run(app, host=args.host, port=args.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
