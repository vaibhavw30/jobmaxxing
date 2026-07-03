from jobmaxxing.normalize import canonicalize_url


def test_indeed_keeps_jk_query():
    assert canonicalize_url("https://www.indeed.com/viewjob?jk=abc123&utm=x") == \
        "https://www.indeed.com/viewjob?jk=abc123&utm=x"

def test_glassdoor_keeps_query():
    assert canonicalize_url("https://www.glassdoor.com/job-listing/x?jl=999") == \
        "https://www.glassdoor.com/job-listing/x?jl=999"

def test_non_identity_host_still_strips_query():
    # unchanged behavior for the existing sources
    assert canonicalize_url("https://simplify.jobs/p/x?utm_source=g") == "https://simplify.jobs/p/x"

def test_linkedin_path_identity_unaffected():
    assert canonicalize_url("https://www.linkedin.com/jobs/view/12345?trk=y") == \
        "https://www.linkedin.com/jobs/view/12345"

def test_schemeless_returned_unchanged():
    assert canonicalize_url("indeed.com/viewjob?jk=z") == "indeed.com/viewjob?jk=z"
