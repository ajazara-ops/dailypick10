"""Single-attempt existing Expo path. Tokens remain only in recipient memory."""
import re

EXPO_URL = 'https://exp.host/--/api/v2/push/send'
TOKEN = re.compile(r'(?:Exponent|Expo)PushToken\[[^\s\[\]]+\]')


class ExpoNotifier:
    def __init__(self, token_source, post):
        self.token_source, self.post = token_source, post

    def send(self, key, title, body):
        if not re.fullmatch(r'daily-[0-9a-f]{64}', key):
            raise ValueError('invalid_notification_key')
        # DailyPublisher calls this only after the durable claim is confirmed.
        try:
            raw = self.token_source()
            if not isinstance(raw, list) or any(not isinstance(t, str) or not TOKEN.fullmatch(t) for t in raw):
                return 'unknown'
            tokens = list(dict.fromkeys(raw))
        except Exception:
            return 'unknown'
        if not tokens:
            return 'no_recipients'
        rejected = False
        for token in tokens:
            payload = {'to': token, 'title': title, 'body': body,
                       'data': {'title': title, 'message': body, 'notificationKey': key},
                       'sound': 'default', 'priority': 'high', 'channelId': 'default',
                       'badge': 1, '_displayInForeground': True}
            try:
                response = self.post(EXPO_URL, json=payload,
                    headers={'accept': 'application/json', 'content-type': 'application/json'},
                    timeout=15, allow_redirects=False)
                if response.status_code != 200:
                    return 'unknown'  # Includes rate limit: stop, no retries/rotation.
                data = response.json()
                ticket = data.get('data') if isinstance(data, dict) else None
                if isinstance(ticket, list) and len(ticket) == 1:
                    ticket = ticket[0]
                if not isinstance(ticket, dict) or data.get('errors'):
                    return 'unknown'
                if ticket.get('status') == 'error':
                    rejected = True
                elif ticket.get('status') != 'ok' or not isinstance(ticket.get('id'), str) or not ticket['id']:
                    return 'unknown'
            except Exception:
                return 'unknown'  # The previous request may already be accepted.
        return 'rejected' if rejected else 'accepted'
