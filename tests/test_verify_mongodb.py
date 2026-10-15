import io
import json
import unittest
from unittest.mock import patch

from verify_mongodb import report_explain


class FakeResponse:
    def __init__(self, payload):
        self.payload = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.payload.close()

    def read(self, *args):
        return self.payload.read(*args)


class ExplainVerifierTests(unittest.TestCase):
    def test_reports_pass_only_for_index_scan_without_collection_scan(self):
        payload = {
            "explain": {
                "queryPlanner": {
                    "winningPlan": {
                        "stage": "FETCH",
                        "inputStage": {
                            "stage": "IXSCAN",
                            "indexName": "date_-1_emp_code_1",
                        },
                    }
                }
            }
        }
        with patch(
            "verify_mongodb.urlopen",
            return_value=FakeResponse(payload),
        ):
            self.assertTrue(report_explain("http://localhost", "attendance_list"))

    def test_reports_failure_for_collection_scan_even_when_route_returns_200(self):
        payload = {
            "explain": {
                "queryPlanner": {"winningPlan": {"stage": "COLLSCAN"}}
            }
        }
        with patch(
            "verify_mongodb.urlopen",
            return_value=FakeResponse(payload),
        ):
            self.assertFalse(report_explain("http://localhost", "attendance_list"))

    def test_reports_failure_when_explain_has_no_index_scan(self):
        payload = {
            "explain": {
                "queryPlanner": {"winningPlan": {"stage": "EOF"}}
            }
        }
        with patch(
            "verify_mongodb.urlopen",
            return_value=FakeResponse(payload),
        ):
            self.assertFalse(report_explain("http://localhost", "attendance_list"))


if __name__ == "__main__":
    unittest.main()
