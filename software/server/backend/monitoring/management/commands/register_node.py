"""Create a node if it does not exist yet (used by start_demo.bat).

    python manage.py register_node esp32cam-01 --name "ESP32-S3 CAM"
"""

from django.core.management.base import BaseCommand

from monitoring.models import Node


class Command(BaseCommand):
    help = 'Register a node by code if it is not registered yet.'

    def add_arguments(self, parser):
        parser.add_argument('code')
        parser.add_argument('--name', default='')

    def handle(self, *args, code, name, **options):
        node, created = Node.objects.get_or_create(code=code, defaults={'name': name})
        self.stdout.write(f'Node {node.code}: {"registered" if created else "already registered"}.')
