from jobmaxxing.tailoring.scorer import delta, score

RUBRIC = {
    "keyword_dict": ["c++", "low-latency", "backtesting", "kubernetes"],
    "aliases": {"c++": ["cpp"], "kubernetes": ["k8s"]},
}


def test_static_coverage_counts_dict_terms_in_resume():
    s = score(resume_text="I write cpp and do backtesting", jd_text="", rubric=RUBRIC)
    # cpp (alias of c++) + backtesting => 2 of 4
    assert s["static"] == 0.5
    assert set(s["matched"]) == {"c++", "backtesting"}


def test_dynamic_coverage_is_jd_conditioned():
    # JD asks for c++ and low-latency; resume has only c++
    s = score(resume_text="experienced in c++", jd_text="must know c++ and low-latency", rubric=RUBRIC)
    assert s["dynamic"] == 0.5                      # 1 of the 2 JD-mentioned terms covered
    assert s["missing"] == ["low-latency"]          # JD wants it, resume lacks it


def test_dynamic_is_one_when_jd_mentions_no_dict_terms():
    s = score(resume_text="c++", jd_text="we like teamwork and coffee", rubric=RUBRIC)
    assert s["dynamic"] == 1.0
    assert s["missing"] == []


def test_boundary_aware_alias_matching():
    # 'k8s' alias matches; bare 'cpp' must not match inside 'cppfoo'
    s = score(resume_text="deploy on k8s, cppfoo is irrelevant", jd_text="", rubric=RUBRIC)
    assert "kubernetes" in s["matched"]
    assert "c++" not in s["matched"]


def test_empty_dict_is_zero_static():
    s = score(resume_text="anything", jd_text="anything", rubric={"keyword_dict": [], "aliases": {}})
    assert s["static"] == 0.0 and s["dynamic"] == 1.0


def test_delta_subtracts_axes():
    before = {"static": 0.4, "dynamic": 0.5}
    after = {"static": 0.7, "dynamic": 0.9}
    assert delta(before, after) == {"static": 0.3, "dynamic": 0.4}
