from django.core.management.base import BaseCommand

from Dashboard.notifications import (build_reminder_text, line_configured,
                                     send_line_message)


class Command(BaseCommand):
    help = ('Build the staff reminder summary (due/overdue follow-ups and orders) '
            'and push it to LINE when credentials are configured. '
            'Use --dry-run to print the message without sending.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Print the reminder text without sending')

    def handle(self, *args, **options):
        text = build_reminder_text()
        if options['dry_run']:
            self.stdout.write(text)
            return
        if not line_configured():
            self.stdout.write(self.style.WARNING(
                'LINE is not configured - set LINE_CHANNEL_ACCESS_TOKEN and LINE_TARGET_ID. '
                'Reminder text:\n\n' + text))
            return
        result = send_line_message(text)
        if result['sent']:
            self.stdout.write(self.style.SUCCESS('LINE reminder sent.'))
        else:
            self.stderr.write(self.style.ERROR(f"LINE send failed: {result['reason']}"))
