from __future__ import annotations

import argparse
import sys
from typing import TextIO

from fastapi import FastAPI, HTTPException


app = FastAPI(title="GPU Server Manager Codex MCP Agent", version="0.3.0-rc.4")


@app.get("/codex-mcp/health")
def health() -> dict[str, str]:
    return {"status": "disabled"}


@app.get("/codex-mcp/tools")
def tools():
    raise HTTPException(
        status_code=410,
        detail="Codex MCP is disabled; use Web or WeChat",
    )


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
) -> int:
    parser = argparse.ArgumentParser(description="GPU Server Manager Codex MCP Agent")
    parser.add_argument("--stdio", action="store_true", help="Run as an MCP stdio server")
    args = parser.parse_args(argv)
    if args.stdio:
        print("Codex MCP is disabled; use Web or WeChat.", file=stdout)
        return 2
    parser.print_help(file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
