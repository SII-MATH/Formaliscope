"""Read attributed Blueprint expectations independently of Lean readbacks."""
from copy import deepcopy

CONTEXT_SCHEMA = 'formaliscope-blueprint-expectations.v1'


def expectation_context(snapshot: dict, selected: list[str], additional: str | None = None) -> dict:
    cards = {card['id']: card for card in snapshot['cards']}
    return {'schema': CONTEXT_SCHEMA, 'source_commit': snapshot['source_commit'],
            'snapshot_digest': snapshot['digest'],
            'references': {identity: card_references(cards[identity]) for identity in selected},
            'additional_context': additional}


def card_references(card: dict) -> list[dict]:
    if 'blueprint_references' in card:
        return deepcopy(card['blueprint_references'])
    # Older base snapshots kept only their first Blueprint text in statement.
    if card.get('statement_origin') == 'blueprint' and card.get('statement', '').strip():
        return [{'title': card.get('title', ''), 'statement': card['statement'],
                 'label': card.get('label', ''), 'chapter': card.get('chapter', ''),
                 'blueprint_file': card['blueprint_file'],
                 'blueprint_line': card['blueprint_line'],
                 'declarations': [card['declaration']]}]
    return []
