from jobmaxxing.tailoring.scorer import delta, score_keywords

RUBRIC = {
    "keyword_dict": ["c++", "low-latency", "backtesting", "kubernetes"],
    "aliases": {"c++": ["cpp"], "kubernetes": ["k8s"]},
}


def test_static_coverage_counts_dict_terms_in_resume():
    s = score_keywords(resume_text="I write cpp and do backtesting", jd_text="", rubric=RUBRIC)
    # cpp (alias of c++) + backtesting => 2 of 4
    assert s["static"] == 0.5
    assert set(s["matched"]) == {"c++", "backtesting"}


def test_dynamic_coverage_is_jd_conditioned():
    # JD asks for c++ and low-latency; resume has only c++
    s = score_keywords(resume_text="experienced in c++", jd_text="must know c++ and low-latency", rubric=RUBRIC)
    assert s["dynamic"] == 0.5                      # 1 of the 2 JD-mentioned terms covered
    assert s["missing"] == ["low-latency"]          # JD wants it, resume lacks it


def test_dynamic_is_one_when_jd_mentions_no_dict_terms():
    s = score_keywords(resume_text="c++", jd_text="we like teamwork and coffee", rubric=RUBRIC)
    assert s["dynamic"] == 1.0
    assert s["missing"] == []


def test_boundary_aware_alias_matching():
    # 'k8s' alias matches; bare 'cpp' must not match inside 'cppfoo'
    s = score_keywords(resume_text="deploy on k8s, cppfoo is irrelevant", jd_text="", rubric=RUBRIC)
    assert "kubernetes" in s["matched"]
    assert "c++" not in s["matched"]


def test_empty_dict_is_zero_static():
    s = score_keywords(resume_text="anything", jd_text="anything", rubric={"keyword_dict": [], "aliases": {}})
    assert s["static"] == 0.0 and s["dynamic"] == 1.0


def test_delta_subtracts_axes():
    before = {"static": 0.4, "dynamic": 0.5}
    after = {"static": 0.7, "dynamic": 0.9}
    d = delta(before, after)
    assert d["static"] == 0.3 and d["dynamic"] == 0.4


def test_multi_word_term_missing_when_jd_wants_it():
    rubric = {"keyword_dict": ["machine learning", "python"], "aliases": {}}
    s = score_keywords(resume_text="I know python", jd_text="we need machine learning", rubric=rubric)
    assert s["missing"] == ["machine learning"]
    assert "machine learning" not in s["matched"]


def test_jd_terms_outside_dict_never_leak_into_missing():
    rubric = {"keyword_dict": ["python"], "aliases": {}}
    s = score_keywords(resume_text="python", jd_text="we need rust and golang", rubric=rubric)
    assert s["missing"] == []          # rust/golang aren't dict terms, so not our concern


def test_none_inputs_do_not_crash():
    rubric = {"keyword_dict": ["python"], "aliases": {}}
    s = score_keywords(resume_text=None, jd_text=None, rubric=rubric)
    assert s["static"] == 0.0 and s["dynamic"] == 1.0 and s["matched"] == [] and s["missing"] == []


def test_non_list_alias_value_is_ignored_not_exploded():
    # a malformed alias (string instead of list) must not spread into single-char patterns
    rubric = {"keyword_dict": ["python"], "aliases": {"python": "py"}}
    s = score_keywords(resume_text="I use py everywhere", jd_text="", rubric=rubric)
    assert s["matched"] == []          # 'py' not credited; no char-level 'p'/'y' matching
