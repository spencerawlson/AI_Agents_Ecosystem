"""Initialize the PostgreSQL schema. Usage: python -m infra.init_db"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine

from infra.db_models import Base

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://ecosystem:ecosystem@localhost:5432/ecosystem"
)


def main() -> None:
    # create_all needs a sync driver; strip +asyncpg if present.
    url = DATABASE_URL.replace("+asyncpg", "")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    print(f"Schema created at {url}")


if __name__ == "__main__":
    main()