from language import detect_language


def test_english_question_is_detected():
    assert detect_language("My Vaillant ecoTEC pro shows fault F.22. What does it mean?") == "en"


def test_dutch_question_is_detected():
    assert detect_language("Vaillant ecoTEC pro geeft F.22, wat moet ik controleren?") == "nl"


def test_ambiguous_message_defaults_to_dutch():
    assert detect_language("F.22") == "nl"
