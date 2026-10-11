"""Single source of truth for the legal entity that operates Carrac.

Every Terms / Privacy / Refund / footer reference to "operated by
Carrac.cc" (a domain, not a legal person) gets replaced with the real
trade name + jurisdiction. The form and jurisdiction are filled in via .env
(LEGAL_ENTITY_FORM / LEGAL_ENTITY_COUNTRY); until then the legal pages
disclose neither, and deploys work without them.

Surfaced to Jinja templates via the `legal_entity` context processor in
carrac/__init__.py.
"""
from __future__ import annotations

import os


from carrac.site_url import support_email  # noqa: E402


def get_legal_entity() -> dict:
    return {
        # Trade name — what shows on Terms §1, footer, Paddle merchant.
        # Override with LEGAL_ENTITY_NAME to show the registered company name.
        'name': os.environ.get('LEGAL_ENTITY_NAME', 'Carrac'),
        # Legal form (e.g. "private limited company"). EMPTY by default: the
        # form/jurisdiction clause is omitted unless both this and the country
        # are set (see disclosure_sentence + privacy.html), after which the full
        # "a [form] registered in [country]" disclosure appears automatically.
        'form': os.environ.get('LEGAL_ENTITY_FORM', ''),
        # Jurisdiction. EMPTY by default; set LEGAL_ENTITY_COUNTRY (e.g. "Ireland").
        'country': os.environ.get('LEGAL_ENTITY_COUNTRY', ''),
        # Address shown on Privacy §1 (Data Controller) and Refund §1.
        'address': os.environ.get('LEGAL_ENTITY_ADDRESS', 'Address on file'),
        # Optional registry / company numbers.
        'registry_number': os.environ.get('LEGAL_ENTITY_REGISTRY_NUMBER', ''),
        'vat_id': os.environ.get('LEGAL_ENTITY_VAT_ID', ''),
        # Contact for legal correspondence.
        'contact_email': os.environ.get('LEGAL_ENTITY_CONTACT_EMAIL') or support_email(),
    }


def disclosure_sentence() -> str:
    """A reusable one-liner suitable for Terms §1 / footer.

    With no form/country set the jurisdiction clause is omitted:
      "Carrac is operated by Carrac."
    Once LEGAL_ENTITY_FORM + LEGAL_ENTITY_COUNTRY are set:
      "Carrac is operated by Example Ltd, a private limited company
       registered in Ireland."
    """
    e = get_legal_entity()
    if e['form'] and e['country']:
        return (
            f"Carrac is operated by {e['name']}, "
            f"a {e['form']} registered in {e['country']}."
        )
    return f"Carrac is operated by {e['name']}."
