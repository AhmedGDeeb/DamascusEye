"""Compile locale/*/LC_MESSAGES/django.po into .mo files without GNU gettext.

Django's own `compilemessages` needs the gettext tools, which are not
installed on Windows by default. This does the same job in pure Python:

    python manage.py compile_translations

Run it after editing a .po file, then restart the server.
"""

import ast
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def parse_po(path):
    """Return {msgid: msgstr} for translated, non-fuzzy entries (plus the header)."""
    entries = {}
    msgid = msgstr = section = None
    fuzzy = next_fuzzy = False

    def flush():
        # The header (msgid "") is kept even when marked fuzzy.
        if msgid is not None and msgstr and (not fuzzy or msgid == ''):
            entries[msgid] = msgstr

    for number, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#,') and 'fuzzy' in line:
            next_fuzzy = True
            continue
        if line.startswith('#'):
            continue
        try:
            if line.startswith('msgid '):
                flush()
                msgid, msgstr, section = ast.literal_eval(line[6:]), None, 'msgid'
                fuzzy, next_fuzzy = next_fuzzy, False
                continue
            if line.startswith('msgstr '):
                msgstr, section = ast.literal_eval(line[7:]), 'msgstr'
                continue
            if line.startswith('"'):
                text = ast.literal_eval(line)
                if section == 'msgid':
                    msgid += text
                elif section == 'msgstr':
                    msgstr += text
                continue
        except (SyntaxError, ValueError):
            pass
        raise CommandError(f'{path}:{number}: cannot parse line: {raw}')
    flush()
    return entries


def write_mo(entries, path):
    """GNU .mo format: header, two offset tables, then the strings."""
    keys = sorted(entries)
    ids = [k.encode('utf-8') for k in keys]
    strs = [entries[k].encode('utf-8') for k in keys]
    count = len(keys)
    ids_start = 7 * 4 + count * 16
    strs_start = ids_start + sum(len(i) + 1 for i in ids)

    id_table, str_table = [], []
    offset = ids_start
    for item in ids:
        id_table += [len(item), offset]
        offset += len(item) + 1
    offset = strs_start
    for item in strs:
        str_table += [len(item), offset]
        offset += len(item) + 1

    output = struct.pack('<7I', 0x950412DE, 0, count, 7 * 4, 7 * 4 + count * 8, 0, 0)
    output += struct.pack(f'<{len(id_table)}I', *id_table)
    output += struct.pack(f'<{len(str_table)}I', *str_table)
    output += b''.join(item + b'\0' for item in ids)
    output += b''.join(item + b'\0' for item in strs)
    path.write_bytes(output)


class Command(BaseCommand):
    help = 'Compile .po translation files to .mo (no GNU gettext needed).'

    def handle(self, *args, **options):
        compiled = 0
        for locale_dir in settings.LOCALE_PATHS:
            for po in sorted(Path(locale_dir).glob('*/LC_MESSAGES/*.po')):
                entries = parse_po(po)
                write_mo(entries, po.with_suffix('.mo'))
                compiled += 1
                self.stdout.write(f'{po}: {len(entries) - ("" in entries)} messages')
        if not compiled:
            raise CommandError('No .po files found in LOCALE_PATHS.')
        self.stdout.write(self.style.SUCCESS(f'Compiled {compiled} file(s).'))
