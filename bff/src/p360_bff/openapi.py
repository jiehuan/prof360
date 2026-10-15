"""Write docs/bff-openapi.json, the OpenAPI document of the BFF, for the front end.

python -m p360_bff.openapi
"""

from __future__ import annotations

import json

from sqlalchemy import create_engine

from p360_bff.app import create_app
from p360_bff.settings import REPO, Settings

OUT = REPO / "docs" / "bff-openapi.json"


def main() -> None:
    app = create_app(Settings(), create_engine("sqlite://"))  # the spec needs no data
    OUT.write_text(json.dumps(app.openapi(), indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
