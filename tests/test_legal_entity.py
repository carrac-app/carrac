"""Legal-entity disclosure posture.

The legal pages disclose the entity form and jurisdiction only when
LEGAL_ENTITY_FORM / LEGAL_ENTITY_COUNTRY are set. This test guards against a
change that discloses a jurisdiction when they are unset.
"""
import carrac.legal_entity as le


def test_disclosure_minimal_when_no_form_or_country(monkeypatch):
    monkeypatch.delenv('LEGAL_ENTITY_FORM', raising=False)
    monkeypatch.delenv('LEGAL_ENTITY_COUNTRY', raising=False)
    e = le.get_legal_entity()
    assert e['form'] == '' and e['country'] == ''
    s = le.disclosure_sentence()
    assert s == 'Carrac is operated by Carrac.'
    assert 'sole proprietor' not in s and 'registered in' not in s


def test_disclosure_full_when_env_set(monkeypatch):
    monkeypatch.setenv('LEGAL_ENTITY_NAME', 'Example Ltd')
    monkeypatch.setenv('LEGAL_ENTITY_FORM', 'private limited company')
    monkeypatch.setenv('LEGAL_ENTITY_COUNTRY', 'Ireland')
    assert le.disclosure_sentence() == (
        'Carrac is operated by Example Ltd, a private limited company '
        'registered in Ireland.'
    )


def test_no_hardcoded_jurisdiction_in_public_legal_templates():
    """Public legal pages must not hard-code a jurisdiction: a "registered in
    <place>" clause may only come from the legal_entity variables, which are
    empty unless configured."""
    import pathlib
    import re
    tpl = pathlib.Path(__file__).resolve().parent.parent / 'templates'
    for name in ('terms.html', 'disclaimer.html', 'privacy.html'):
        txt = (tpl / name).read_text(encoding='utf-8')
        # Drop Jinja expressions/statements, leaving only literal page text.
        literal = re.sub(r'\{\{.*?\}\}|\{%.*?%\}', '', txt, flags=re.S)
        assert not re.search(r'registered in\s+[A-Z]', literal), (
            f'{name} hard-codes a "registered in <place>" clause'
        )
