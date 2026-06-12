def test_tailor_entrypoint_module_resolves():
    import jobmaxxing.tailor as entry
    from jobmaxxing.tailoring.tailor import main as impl_main
    assert entry.main is impl_main


def test_main_is_callable():
    from jobmaxxing.tailoring.tailor import main
    assert callable(main)
