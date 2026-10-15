"""`p360-bff` / `python -m p360_bff`: serve the API (development). Production runs uvicorn
from the container (deploy/openshift/bff.yaml)."""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    uvicorn.run("p360_bff.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
