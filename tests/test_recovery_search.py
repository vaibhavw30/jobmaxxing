from jobmaxxing.recovery.search import build_query, ddg_search


def test_build_query():
    assert build_query("Chegg", "Computational Linguist") == "Chegg Computational Linguist"
    assert build_query(None, "ML Intern") == "ML Intern"


_DDG_HTML = """
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fglassdoor.com%2Fjob%2F1&rut=x">Glassdoor</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ftrimble.wd1.myworkdayjobs.com%2Fx&rut=y">Workday</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fjobgether.com%2Fp%2F2&rut=z">Jobgether</a>
"""


def test_ddg_search_unwraps_and_filters_workday():
    seen = {}
    def fake_fetch(url):
        seen["url"] = url
        return _DDG_HTML
    results = ddg_search("Chegg Computational Linguist", fetch_text=fake_fetch)
    assert "html.duckduckgo.com" in seen["url"]
    assert results == ["https://glassdoor.com/job/1", "https://jobgether.com/p/2"]   # workday filtered out


def test_ddg_search_respects_max_results():
    def fake_fetch(url):
        return _DDG_HTML
    assert len(ddg_search("q", fetch_text=fake_fetch, max_results=1)) == 1
