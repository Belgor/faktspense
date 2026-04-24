import fakturoid_naklady


def test_version_exposed() -> None:
    assert fakturoid_naklady.__version__ == "0.1.0"
