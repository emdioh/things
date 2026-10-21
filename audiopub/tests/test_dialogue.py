from audiopub.dialogue import split_paragraph


def spans(text):
    return [(s.text, s.dialogue) for s in split_paragraph(text)]


def test_curly_quotes():
    assert spans("“Where are you going?” asked Anna.") == [("Where are you going?", True), ("asked Anna.", False)]


def test_two_quotes_with_tag_between():
    assert spans("“To the market,” Marco replied. “Do you want to come?”") == [
        ("To the market,", True), ("Marco replied.", False), ("Do you want to come?", True)
    ]


def test_straight_quotes_and_narration_only():
    assert spans('He said "hi" softly.') == [("He said", False), ("hi", True), ("softly.", False)]
    assert spans("No dialogue here.") == [("No dialogue here.", False)]


def test_guillemets():
    assert spans("«Ciao» disse Marco.") == [("Ciao", True), ("disse Marco.", False)]


def test_dash_dialogue():
    assert spans("— Ciao — disse Marco — come stai?") == [
        ("Ciao", True), ("disse Marco", False), ("come stai?", True)
    ]


def test_apostrophes_are_not_quotes():
    assert spans("It’s Anna’s book.") == [("It’s Anna’s book.", False)]
    assert spans("‘Don’t go,’ she said.") == [("Don’t go,", True), ("she said.", False)]


def test_unclosed_quote_runs_to_end():
    assert spans("“And then we left") == [("And then we left", True)]
