from jobmaxxing.recovery.extract import JobPosting, extract_job_posting, workday_req_id


def test_workday_req_id():
    assert workday_req_id("https://x.wd1.myworkdayjobs.com/Ext/job/NYC/SW-Intern_JR012226") == "JR012226"
    assert workday_req_id("https://x.wd3.myworkdayjobs.com/C/job/Loc/Renewable-Eng_R-1289") == "R-1289"
    assert workday_req_id("https://x.wd1.myworkdayjobs.com/C/job/Loc/CV-Intern_REQ-4012") == "REQ-4012"
    assert workday_req_id("https://job-boards.greenhouse.io/acme/jobs/123") is None


_HTML = """<html><head>
<script type="application/ld+json">
{"@type":"JobPosting","title":"ML Intern","description":"<p>Build models</p>",
 "hiringOrganization":{"@type":"Organization","name":"Chegg"},
 "identifier":{"@type":"PropertyValue","name":"req","value":"JR012226"},
 "url":"https://glassdoor.com/job/123"}
</script></head><body>x</body></html>"""


def test_extract_top_level_job_posting():
    jp = extract_job_posting(_HTML, source_url="https://glassdoor.com/job/123")
    assert isinstance(jp, JobPosting)
    assert jp.description == "<p>Build models</p>"
    assert jp.title == "ML Intern"
    assert jp.company == "Chegg"               # hiringOrganization object -> name
    assert jp.identifier == "JR012226"         # identifier object -> value
    assert jp.url == "https://glassdoor.com/job/123"
    assert jp.source_url == "https://glassdoor.com/job/123"


def test_extract_graph_and_string_forms():
    html = ('<script type="application/ld+json">'
            '{"@graph":[{"@type":"WebPage"},'
            '{"@type":"JobPosting","description":"d","hiringOrganization":"Acme Corp","identifier":"R-9"}]}'
            '</script>')
    jp = extract_job_posting(html)
    assert jp.description == "d" and jp.company == "Acme Corp" and jp.identifier == "R-9"


def test_extract_returns_none_when_absent_or_no_description():
    assert extract_job_posting("<html>no ld json</html>") is None
    assert extract_job_posting('<script type="application/ld+json">{"@type":"JobPosting"}</script>') is None
    assert extract_job_posting('<script type="application/ld+json">not json</script>') is None
