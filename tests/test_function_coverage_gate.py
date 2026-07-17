import pytest

from tests import check_function_coverage


def _report(covered, statements, missing=()):
    return {
        "files": {
            "utility.py": {
                "functions": {
                    "work": {
                        "start_line": 10,
                        "summary": {
                            "covered_lines": covered,
                            "num_statements": statements,
                        },
                        "missing_lines": list(missing),
                    },
                    "": {
                        "start_line": 1,
                        "summary": {
                            "covered_lines": 0,
                            "num_statements": 20,
                        },
                        "missing_lines": list(range(1, 21)),
                    },
                }
            }
        }
    }


def test_evaluate_report_accepts_threshold_boundary():
    failures, inspected = check_function_coverage.evaluate_report(
        _report(covered=4, statements=5),
        80,
    )

    assert failures == []
    assert inspected == 1


def test_evaluate_report_names_function_and_missing_lines():
    failures, inspected = check_function_coverage.evaluate_report(
        _report(covered=3, statements=5, missing=(12, 14)),
        80,
    )

    assert inspected == 1
    assert failures == [
        "utility.py:10 work: 60.00% (3/5); missing 12,14"
    ]


def test_evaluate_report_requires_function_regions():
    with pytest.raises(ValueError, match="per-function data"):
        check_function_coverage.evaluate_report(
            {"files": {"utility.py": {}}},
            80,
        )
