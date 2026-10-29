"""Automated tests.

Run with either:
    python -m unittest discover -s tests -v
    pytest -v

Strategy: expected values are computed INDEPENDENTLY with pandas straight from
the CSV, then compared with what the API returns (which uses SQLite). If the
two ever disagree, a test fails.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from app import create_app
from bi.database import build_database, get_readonly_connection
from bi.formatting import inr
from bi.llm import LLMError, make_llm_func, parse_json_object

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "sales.csv"


def expected_frame() -> pd.DataFrame:
    """Independent pandas calculation of the 'valid + completed' sales."""
    df = pd.read_csv(CSV_PATH)
    df["Order_date"] = pd.to_datetime(df["Order_date"], format="%d-%m-%Y")
    df["month"] = df["Order_date"].dt.strftime("%Y-%m")
    df = df[(df["Quantity"] > 0) & df["Unit_price"].notna() & (df["Status"] == "Completed")].copy()
    df["revenue"] = df["Quantity"] * df["Unit_price"] - df["Discount_amount"]
    return df


class BaseCase(unittest.TestCase):
    llm_func = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "test.db"
        cls.app = create_app({"CSV_PATH": CSV_PATH, "DB_PATH": cls.db_path, "LLM_FUNC": cls.llm_func})
        cls.client = cls.app.test_client()
        cls.df = expected_frame()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ask(self, question):
        response = self.client.post("/api/ask", json={"question": question})
        return response.status_code, response.get_json()


class TestValidQuestions(BaseCase):
    def test_total_revenue_matches_pandas(self):
        status, body = self.ask("What is the total revenue?")
        self.assertEqual(status, 200)
        net = body["supporting_data"]["rows"][0][4]
        self.assertAlmostEqual(net, self.df["revenue"].sum(), places=2)
        self.assertIn("SELECT", body["sql"])

    def test_top_product_and_region_match_pandas(self):
        status, body = self.ask("Which product generated the highest revenue?")
        self.assertEqual(status, 200)
        self.assertEqual(body["supporting_data"]["rows"][0][0], self.df.groupby("Product")["revenue"].sum().idxmax())
        status, body = self.ask("Which region generated the highest sales?")
        self.assertEqual(body["supporting_data"]["rows"][0][0], self.df.groupby("Region")["revenue"].sum().idxmax())

    def test_monthly_trend_matches_pandas_and_sums_to_total(self):
        status, body = self.ask("Show the monthly revenue trend")
        self.assertEqual(status, 200)
        rows = body["supporting_data"]["rows"]
        expected = self.df.groupby("month")["revenue"].sum()
        self.assertEqual([r[0] for r in rows], list(expected.index))
        self.assertAlmostEqual(sum(r[1] for r in rows), self.df["revenue"].sum(), places=2)

    def test_month_comparison(self):
        status, body = self.ask("How did revenue change between January and February?")
        self.assertEqual(status, 200)
        by_month = self.df.groupby("month")["revenue"].sum()
        rows = body["supporting_data"]["rows"]
        self.assertEqual([r[0] for r in rows], ["2026-01", "2026-02"])
        self.assertAlmostEqual(rows[0][1], by_month["2026-01"], places=2)
        self.assertAlmostEqual(rows[1][1], by_month["2026-02"], places=2)

    def test_filter_by_region(self):
        status, body = self.ask("What is the total revenue for the North region?")
        self.assertEqual(status, 200)
        north = self.df[self.df["Region"] == "North"]["revenue"].sum()
        self.assertAlmostEqual(body["supporting_data"]["rows"][0][4], north, places=2)
        self.assertEqual(body["interpretation"]["filters"], {"region": "North"})

    def test_lowest_category(self):
        status, body = self.ask("What is the lowest revenue category?")
        self.assertEqual(status, 200)
        self.assertEqual(body["supporting_data"]["rows"][0][0], self.df.groupby("Category")["revenue"].sum().idxmin())


class TestInvalidAndEdgeCases(BaseCase):
    def test_empty_question_is_400(self):
        status, body = self.ask("   ")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_bad_json_body_is_400(self):
        response = self.client.post("/api/ask", data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_unrelated_question_is_refused(self):
        status, body = self.ask("What is the weather in Hyderabad today?")
        self.assertEqual(status, 422)
        self.assertTrue(body["error"]["suggestions"])

    def test_unsupported_metric_is_refused_not_guessed(self):
        status, body = self.ask("What is our profit margin?")
        self.assertEqual(status, 422)

    def test_destructive_request_is_blocked_and_data_untouched(self):
        before = self._row_count()
        status, body = self.ask("Delete all sales records")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "read_only")
        self.assertEqual(self._row_count(), before)

    def test_month_without_data_edge_case(self):
        status, body = self.ask("What is the total revenue in July?")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "data_not_available")

    def test_unknown_filter_value_not_silently_ignored_by_validator(self):
        from bi.database import get_catalog
        from bi.planner import validate_plan
        from bi.errors import QuestionError
        with get_readonly_connection(self.db_path) as conn:
            catalog = get_catalog(conn)
        with self.assertRaises(QuestionError):
            validate_plan({"intent": "total_revenue", "filters": {"product": "Smartphone"}}, catalog)

    def test_unknown_route_returns_json_404(self):
        response = self.client.get("/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"]["code"], "not_found")

    def _row_count(self):
        with get_readonly_connection(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]


class TestDataManagement(BaseCase):
    def test_bad_rows_are_rejected_with_reasons(self):
        response = self.client.get("/api/data-quality")
        body = response.get_json()
        rejected = {r["transaction_id"]: r["reason"] for r in body["rejected_records"]}
        self.assertIn("TXN0034", rejected)   # quantity = 0
        self.assertIn("TXN0051", rejected)   # missing unit price
        self.assertIn("Quantity", rejected["TXN0034"])
        self.assertIn("Unit_price", rejected["TXN0051"])
        self.assertEqual(body["data_load"], {"total_rows": 51, "loaded": 49, "rejected": 2})

    def test_returned_order_stored_but_excluded_from_revenue(self):
        with get_readonly_connection(self.db_path) as conn:
            status = conn.execute("SELECT status FROM sales WHERE transaction_id='TXN0033'").fetchone()[0]
        self.assertEqual(status, "Returned")
        self.assertEqual(len(self.df), 48)  # 51 - 2 invalid - 1 returned

    def test_database_connection_is_read_only(self):
        with get_readonly_connection(self.db_path) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM sales")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DROP TABLE sales")

    def test_build_database_handles_duplicates_and_bad_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = Path(tmp) / "x.csv"
            csv.write_text(
                "Transaction_Id,Order_date,Product,Category,Region,Sales_channel,Quantity,Unit_price,Discount_amount,Status\n"
                "A1,01-01-2026,P,C,North,Online,1,100,0,Completed\n"
                "A1,02-01-2026,P,C,North,Online,1,100,0,Completed\n"
                "A2,31-02-2026,P,C,North,Online,1,100,0,Completed\n"
                "A3,03-01-2026,P,C,North,Online,1,100,500,Completed\n")
            summary = build_database(csv, Path(tmp) / "x.db")
        self.assertEqual(summary, {"total_rows": 4, "loaded": 1, "rejected": 3})


class TestLLMIntegration(unittest.TestCase):
    """The LLM is faked: tests must never depend on the network."""

    def _client(self, llm_func):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = create_app({"CSV_PATH": CSV_PATH, "DB_PATH": Path(tmp.name) / "t.db", "LLM_FUNC": llm_func})
        return app.test_client()

    def test_valid_llm_plan_is_used(self):
        client = self._client(lambda q, c: {"intent": "ranking", "dimension": "region", "order": "desc"})
        body = client.post("/api/ask", json={"question": "who is winning geographically?"}).get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["interpretation"]["interpreted_by"], "llm")

    def test_malicious_llm_output_is_rejected_then_rules_fallback(self):
        client = self._client(lambda q, c: {"intent": "run_sql", "sql": "DROP TABLE sales"})
        body = client.post("/api/ask", json={"question": "What is the total revenue?"}).get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["interpretation"]["interpreted_by"], "rules_fallback")

    def test_llm_failure_falls_back_to_rules(self):
        def broken(question, catalog):
            raise LLMError("timeout")
        client = self._client(broken)
        body = client.post("/api/ask", json={"question": "Show the monthly revenue trend"}).get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["interpretation"]["interpreted_by"], "rules_fallback")

    def test_hallucinated_product_from_llm_is_rejected(self):
        client = self._client(lambda q, c: {"intent": "total_revenue", "filters": {"product": "Smartphone"}})
        response = client.post("/api/ask", json={"question": "how much money did smartphones bring"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"]["code"], "data_not_available")

    def test_destructive_question_never_reaches_llm(self):
        called = []
        client = self._client(lambda q, c: called.append(q) or {"intent": "total_revenue"})
        response = client.post("/api/ask", json={"question": "drop the sales table"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(called, [])

    def test_parse_json_object_handles_markdown_fences(self):
        self.assertEqual(parse_json_object('```json\n{"intent": "total_revenue"}\n```'), {"intent": "total_revenue"})
        with self.assertRaises(LLMError):
            parse_json_object("sorry, I cannot help")

    def test_provider_call_is_parsed_correctly(self):
        fake = mock.Mock()
        fake.json.return_value = {"candidates": [{"content": {"parts": [{"text": '{"intent":"total_revenue"}'}]}}]}
        fake.raise_for_status.return_value = None
        settings = {"provider": "gemini", "api_key": "k", "model": "m", "base_url": "", "timeout": 5}
        with mock.patch("bi.llm.requests.post", return_value=fake) as post:
            func = make_llm_func(settings)
            self.assertEqual(func("total revenue?", {"months": ["2026-01"], "values": {}}), {"intent": "total_revenue"})
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "k")

    def test_no_provider_means_no_llm(self):
        self.assertIsNone(make_llm_func({"provider": "none", "api_key": "", "model": "", "base_url": "", "timeout": 5}))


class TestFormatting(unittest.TestCase):
    def test_inr_grouping(self):
        self.assertEqual(inr(1234567.5), "₹12,34,567.50")
        self.assertEqual(inr(999), "₹999.00")
        self.assertEqual(inr(-1500), "-₹1,500.00")



class TestAmbiguityGuards(BaseCase):
    def test_unknown_entity_is_not_silently_ignored(self):
        status, body = self.ask("What is the total revenue for Smartphone?")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "data_not_available")
        self.assertIn("smartphone", body["error"]["message"].lower())

    def test_month_range_without_compare_word_asks_for_clarification(self):
        status, body = self.ask("revenue from jan to march")
        self.assertEqual(status, 422)

    def test_each_category_is_a_breakdown_not_a_total(self):
        status, body = self.ask("revenue of each category")
        self.assertEqual(status, 200)
        self.assertEqual(body["interpretation"]["intent"], "ranking")
        self.assertEqual(len(body["supporting_data"]["rows"]), 3)

    def test_top_n_is_refused_instead_of_silently_returning_top_1(self):
        status, body = self.ask("Top 3 products by revenue")
        self.assertEqual(status, 422)

    def test_known_entities_still_work(self):
        for question in ("Total revenue for the Desk product", "revenue for the USB-C Hub",
                         "Total revenue in the North region", "revenue for all products"):
            status, body = self.ask(question)
            self.assertEqual(status, 200, question)


if __name__ == "__main__":
    unittest.main()
