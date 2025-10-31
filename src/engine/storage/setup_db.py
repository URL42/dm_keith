"""CLI helper to initialize the Dungeon Master Keith SQLite database."""

from __future__ import annotations

from .sqlite import SQLiteStore


def main() -> None:
    store = SQLiteStore()
    store.migrate()
    print(f"SQLite database ready at {store.path}")


if __name__ == "__main__":
    main()
