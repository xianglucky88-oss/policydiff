import pytest

def test_clause_is_open_gap():
    from src.policydiff import clause
    with pytest.raises(NotImplementedError):
        clause.process({"id": "x"})

def test_compare_is_open_gap():
    from src.policydiff import compare
    with pytest.raises(NotImplementedError):
        compare.process({"id": "x"})
