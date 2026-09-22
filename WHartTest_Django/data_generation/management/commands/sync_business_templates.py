"""Sync ticket-system biz_* data generation templates into the database."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Sync 智慧AI工单 business templates, deactivate legacy plans, "
        "and refresh API environment admin credentials for token refresh."
    )

    def handle(self, *args, **options):
        script = Path(__file__).resolve().parents[3] / "scripts" / "setup_business_templates.py"
        if not script.is_file():
            raise SystemExit(f"Missing setup script: {script}")
        self.stdout.write(f"Running {script} ...")
        subprocess.run([sys.executable, str(script)], check=True)
