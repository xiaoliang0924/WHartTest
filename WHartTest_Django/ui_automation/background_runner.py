"""Reuse recorder persistence without a connected WebSocket or browser tab."""
from .consumers import UiAutomationConsumer


class _NoNotifications:
    async def group_send(self, *args, **kwargs):
        pass


class BackgroundRecorderRunner(UiAutomationConsumer):
    async def send_json(self, data):
        # Progress and results are persisted by the job/recorder, then polled.
        pass


def create_background_runner(user):
    runner = BackgroundRecorderRunner()
    runner.scope = {"user": user}
    runner.user_id = user.username
    runner.channel_layer = _NoNotifications()
    return runner
