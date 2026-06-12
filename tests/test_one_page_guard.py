from jobmaxxing.tailoring.latex import CompileResult, OnePageResult, enforce_one_page


def _result(pages):
    return CompileResult(pdf_bytes=b"%PDF", page_count=pages, log="")


def test_already_one_page_no_shrink():
    calls = []

    def shrink(tex, pages):
        calls.append(1)
        return tex

    out = enforce_one_page("tex", compile_fn=lambda t: _result(1), shrink_fn=shrink)
    assert isinstance(out, OnePageResult)
    assert out.page_count == 1 and out.retries == 0 and out.fit is True
    assert calls == []                       # shrink never called


def test_shrinks_until_one_page():
    pages = iter([2, 1])                      # first compile 2 pages, after shrink 1 page

    def compile_fn(tex):
        return _result(next(pages))

    def shrink(tex, n):
        return tex + " % cut"

    out = enforce_one_page("tex", compile_fn=compile_fn, shrink_fn=shrink)
    assert out.fit is True and out.page_count == 1 and out.retries == 1


def test_gives_up_after_max_retries_and_flags_not_fit():
    out = enforce_one_page(
        "tex", compile_fn=lambda t: _result(2), shrink_fn=lambda t, n: t, max_retries=3
    )
    assert out.fit is False and out.page_count == 2 and out.retries == 3
