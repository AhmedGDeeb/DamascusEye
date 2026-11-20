"""Every translatable string in the dashboard must have an Arabic translation.

Without GNU gettext, `makemessages` cannot run, so this test plays its role:
it collects msgids the same way Django does (templatize for templates, the
AST for Python) and compares them to locale/ar/LC_MESSAGES/django.po.

To add a string: write it in English in the template or code, add a
msgid/msgstr pair to the .po file, then run `manage.py compile_translations`.
"""

import ast
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils.translation.template import templatize

from dashboard.management.commands.compile_translations import parse_po

BASE = Path(settings.BASE_DIR)
TEMPLATE_DIRS = [BASE / 'dashboard' / 'templates']
PYTHON_FILES = [
    BASE / 'dashboard' / 'forms.py',
    BASE / 'dashboard' / 'views.py',
    BASE / 'dashboard' / 'templatetags' / 'dashboard_tags.py',
    BASE / 'monitoring' / 'models.py',
    BASE / 'monitoring' / 'services.py',
]
GETTEXT_NAMES = {'_', 'gettext', 'gettext_lazy'}
TEMPLATIZED = re.compile(r"""(?:gettext|_)\(u?('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")\)""")


def collect_msgids():
    msgids = {}
    for directory in TEMPLATE_DIRS:
        for path in sorted(directory.rglob('*.html')):
            source = templatize(path.read_text(encoding='utf-8'), origin=str(path))
            for match in TEMPLATIZED.finditer(source):
                msgids.setdefault(ast.literal_eval(match.group(1)), str(path.relative_to(BASE)))
    for path in PYTHON_FILES:
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in GETTEXT_NAMES
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                msgids.setdefault(node.args[0].value, str(path.relative_to(BASE)))
    return msgids


class TranslationTests(SimpleTestCase):
    def test_every_string_has_an_arabic_translation(self):
        catalog = parse_po(BASE / 'locale' / 'ar' / 'LC_MESSAGES' / 'django.po')
        missing = {msgid: where for msgid, where in collect_msgids().items() if msgid not in catalog}
        self.assertEqual(missing, {}, 'Add these to locale/ar/LC_MESSAGES/django.po:')

    def test_placeholders_match(self):
        catalog = parse_po(BASE / 'locale' / 'ar' / 'LC_MESSAGES' / 'django.po')
        placeholder = re.compile(r'%\(\w+\)s')
        for msgid, msgstr in catalog.items():
            if msgid:
                with self.subTest(msgid=msgid):
                    self.assertEqual(sorted(placeholder.findall(msgid)), sorted(placeholder.findall(msgstr)))

    def test_compiled_catalog_is_up_to_date(self):
        po = BASE / 'locale' / 'ar' / 'LC_MESSAGES' / 'django.po'
        mo = po.with_suffix('.mo')
        self.assertTrue(mo.exists() and mo.stat().st_mtime >= po.stat().st_mtime,
                        'Run: python manage.py compile_translations')


@override_settings(LANGUAGE_CODE='ar')
class LanguageSwitchTests(SimpleTestCase):
    def test_login_page_in_both_languages(self):
        arabic = self.client.get(reverse('login'), HTTP_ACCEPT_LANGUAGE='ar').content.decode()
        self.assertIn('dir="rtl"', arabic)
        self.assertIn('تسجيل الدخول', arabic)

        english = self.client.get(reverse('login'), HTTP_ACCEPT_LANGUAGE='en').content.decode()
        self.assertIn('dir="ltr"', english)
        self.assertIn('Log in', english)

    def test_switcher_sets_language_cookie(self):
        response = self.client.post(reverse('set_language'), {'language': 'en', 'next': '/login/'})
        self.assertRedirects(response, '/login/', fetch_redirect_response=False)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, 'en')
