from app.legal_policy import SYSTEM_PROMPT


def test_prompt_forbids_fixed_questionnaire_and_fabrication():
    assert "geen vaste vragenlijst" in SYSTEM_PROMPT.lower()
    assert "voeg geen feiten" in SYSTEM_PROMPT.lower()
    assert "ik deed niets" in SYSTEM_PROMPT.lower()
    assert "organisatie_basisformulier" in SYSTEM_PROMPT.lower()
    assert "mens" in SYSTEM_PROMPT.lower()
