def test_enrich_cli_shim_exposes_main():
    import jobmaxxing.enrich as cli
    from jobmaxxing.enrichment.enrich import main
    assert cli.main is main
