from scripts.kg import stage3_processing_receipt_checkpoint as M
def test_checkpoint_is_filesystem_only_and_requires_terminals():
    source=open(M.__file__).read()
    assert 'get_engine' not in source and 'sqlalchemy' not in source
    assert 'missing terminal at offset' in source
    assert 'write_immutable' in source
