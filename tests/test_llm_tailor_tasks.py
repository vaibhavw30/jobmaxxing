from jobmaxxing.llm.config import candidates_for, load_llm_config


def test_tailor_and_review_tasks_configured():
    cfg = load_llm_config()
    assert candidates_for("tailor", cfg), "tailor task missing"
    assert candidates_for("review", cfg), "review task missing"
    assert candidates_for("route", cfg), "route task must remain"
