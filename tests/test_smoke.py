"""Smoke headless — prova que o núcleo importa sem display/GTK."""


def test_smoke_trivial() -> None:
    assert True


def test_package_version() -> None:
    import hexavante

    assert hexavante.__version__ == "0.1.0"
