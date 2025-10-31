from __future__ import annotations

import io

from src.bots.telegram_bot import summarize_csv


def test_summarize_csv_reports_dimensions() -> None:
    buffer = io.BytesIO()
    buffer.write(b"name,level\nKeith,9\nGoblin,1\n")
    result = summarize_csv(buffer)
    assert "2 rows" in result
    assert "Headers" in result
    assert "name" in result
