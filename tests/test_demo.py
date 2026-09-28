import importlib.util
from pathlib import Path

import pytest

from contractdb.db import connect


def test_demo_financial_records_and_no_overwrite(tmp_path):
    spec = importlib.util.spec_from_file_location("demo", Path(__file__).resolve().parents[1] / "scripts" / "demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    root = tmp_path / "demo"
    app, identifiers = demo.seed(root, "synthetic-test-password")
    assert app.test_client().get("/health").status_code == 200
    with connect(root) as connection:
        rows = connection.execute("SELECT * FROM documents ORDER BY contract_number").fetchall()
        assert len(rows) == 3
        pending = connection.execute("SELECT * FROM documents WHERE id = ?", (identifiers[2],)).fetchone()
        assert pending["review_status"] == "pending"
        assert pending["company_id"] is None and pending["amount_minor"] is None
        first = connection.execute("SELECT * FROM documents WHERE id = ?", (identifiers[0],)).fetchone()
        assert first["amount_minor"] == 1000000 and first["payable_minor"] == 250000
        confirmation = connection.execute("SELECT * FROM payment_confirmations").fetchone()
        assert confirmation["unreceived_minor"] == 600000
        assert confirmation["unpaid_minor"] == 150000
        assert confirmation["uninvoiced_minor"] == 700000
        assert connection.execute("SELECT count(*) FROM finance_records").fetchone()[0] == 4
    before = (root / "contracts.sqlite3").read_bytes()
    with pytest.raises(ValueError, match="empty data directory"):
        demo.seed(root, "synthetic-test-password")
    assert (root / "contracts.sqlite3").read_bytes() == before
