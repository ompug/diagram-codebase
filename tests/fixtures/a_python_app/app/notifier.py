"""Outbound notifications."""

import requests


class WebhookNotifier:
    def send(self, message: str) -> None:
        requests.post("https://hooks.example.com/inventory", json={"text": message}, timeout=5)
