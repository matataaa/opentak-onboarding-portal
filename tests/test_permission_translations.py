"""Every module and function of the permission catalogue has a label in each UI language.

The catalogue API returns keys only; the role editor looks the texts up under
permissions.modules.<module> and permissions.functions.<function, dots as __>.label/.description
(see frontend/src/utils/permissions.js).
"""
import json
from pathlib import Path

import pytest

from app.permissions import catalogue_for_api

LOCALES = Path(__file__).resolve().parent.parent / 'frontend' / 'src' / 'i18n' / 'locales'
LANGUAGES = ('en', 'nl', 'de')


def _catalogue_texts():
    for module in catalogue_for_api():
        yield ('modules', module['module']), None
        for function in module['functions']:
            yield ('functions', function['function'].replace('.', '__')), ('label', 'description')


@pytest.mark.parametrize('language', LANGUAGES)
def test_every_catalogue_entry_is_translated(language):
    texts = json.loads((LOCALES / f'{language}.json').read_text(encoding='utf-8'))['permissions']
    missing = []
    for (section, key), fields in _catalogue_texts():
        entry = texts.get(section, {}).get(key)
        if fields is None:
            if not isinstance(entry, str) or not entry:
                missing.append(f'permissions.{section}.{key}')
            continue
        if not isinstance(entry, dict):
            missing.append(f'permissions.{section}.{key}')
            continue
        # A label is required; a description may be empty but must be there
        if not entry.get('label'):
            missing.append(f'permissions.{section}.{key}.label')
        if not isinstance(entry.get('description'), str):
            missing.append(f'permissions.{section}.{key}.description')
    assert missing == []


def test_no_translations_for_entries_that_left_the_catalogue():
    known_modules = {module['module'] for module in catalogue_for_api()}
    known_functions = {function['function'].replace('.', '__')
                       for module in catalogue_for_api() for function in module['functions']}
    for language in LANGUAGES:
        texts = json.loads((LOCALES / f'{language}.json').read_text(encoding='utf-8'))['permissions']
        assert set(texts['modules']) == known_modules, language
        assert set(texts['functions']) == known_functions, language
