"""Verified transport for credential-bearing notification delivery."""
import httpx

class NotificationHTTPClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs['verify'] = True
        super().__init__(*args, **kwargs)

    def build_request(self, method, url, **kwargs):
        request = super().build_request(method, url, **kwargs)
        if request.url.scheme != 'https':
            raise ValueError('Notification gateways and webhooks require HTTPS')
        return request
