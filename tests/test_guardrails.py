from guardrails import MAX_RESPONSE_LENGTH, SAFETY_WARNING, WEB_DISCLAIMER, is_conversational, validate_response


SOURCED = "🔴 STORING: F.22\n🔧 STAPPEN: vul bij tot 1,5 bar\n📖 BRON: Vaillant ecoTEC pro, pagina 8"


def test_sourced_answer_passes_unchanged():
    assert validate_response(SOURCED) == SOURCED


def test_technical_answer_without_source_is_blocked():
    result = validate_response("Vervang gewoon de drukschakelaar.")
    assert "onvoldoende betrouwbare informatie" in result


def test_conversational_reply_does_not_need_source():
    assert validate_response("Graag gedaan!", requires_source=False) == "Graag gedaan!"


def test_safety_warning_added_for_dangerous_work():
    text = "Koppel de gasleiding los. 📖 BRON: handleiding pagina 4"
    assert validate_response(text).endswith(SAFETY_WARNING)


def test_existing_safety_warning_is_not_duplicated():
    text = "Koppel de gasleiding los. ⚡ VEILIGHEID: sluit eerst het gas af. 📖 BRON: pagina 4"
    assert SAFETY_WARNING not in validate_response(text)


def test_web_answer_gets_disclaimer_instead_of_block():
    result = validate_response("Volgens de fabrikant: controleer de pomp.", from_web=True)
    assert result.endswith(WEB_DISCLAIMER)


def test_long_answer_is_compressed():
    result = validate_response("📖 BRON: pagina 1 " + "x" * 3000)
    assert result.startswith("📖 BRON")
    assert len(result) < MAX_RESPONSE_LENGTH + 200


def test_empty_answer_returns_fallback():
    assert "Probeer de vraag opnieuw" in validate_response("   ")


def test_greeting_is_conversational():
    assert is_conversational("Hallo, bedankt!")


def test_fault_code_is_never_conversational():
    assert not is_conversational("Hallo, ketel geeft storing F.22")
