from scripts import assistant_golden_questions as golden


def test_golden_questions_answer_with_expected_read_only_tools(tmp_path):
    results = golden.run_golden_questions(tmp_path / "assistant_review.sqlite")
    failures = {
        result["id"]: result["failures"]
        for result in results
        if result["failures"]
    }

    assert failures == {}
    assert len(results) == len(golden.GOLDEN_QUESTIONS)
