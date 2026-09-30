from support_desk.__main__ import greet


def test_greet():
    assert greet("Python") == "Привет, Python!"
