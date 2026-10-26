"""Security settings; production must never use public development keys."""
import os
import secrets
import anyio
from urllib.parse import urlparse
from starlette.responses import JSONResponse

RECAPTCHA_TEST_SITE_KEY = "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"
RECAPTCHA_TEST_SECRET_KEY = "6LeIxAcTAAAAAGG-vFI1TnRWxMZNFuojJ4WifJWe"


def is_production():
    return os.getenv("RENDER", "").lower() == "true" or os.getenv("APP_ENV", "").lower() == "production"


def signing_key():
    value = os.getenv("SECRET_KEY", "").strip()
    if len(value) < 32 or value in {
        "nirpr-rso-platform-secret-key-change-in-production",
        "change-this-to-a-long-random-string-in-production",
    }:
        if is_production():
            raise RuntimeError("Set SECRET_KEY to a private random value of at least 32 characters before starting production.")
        # An ephemeral local key is safer than a publicly known signing key.
        return secrets.token_urlsafe(48)
    return value


def recaptcha_settings():
    site = os.getenv("RECAPTCHA_SITE_KEY", "").strip()
    secret = os.getenv("RECAPTCHA_SECRET_KEY", "").strip()
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "") or urlparse(os.getenv("APP_BASE_URL", "")).hostname or ""
    hosts = {item.strip().lower() for item in os.getenv("RECAPTCHA_ALLOWED_HOSTNAMES", host).split(",") if item.strip()}
    test_key = site == RECAPTCHA_TEST_SITE_KEY or secret == RECAPTCHA_TEST_SECRET_KEY
    if is_production() and (not site or not secret or not hosts or test_key):
        raise RuntimeError("Configure real reCAPTCHA v2 keys and RECAPTCHA_ALLOWED_HOSTNAMES before starting production.")
    return site, secret, hosts


class RequestBodyLimitMiddleware:
    """Bound the entire body before multipart parsing can spool files to disk."""
    def __init__(self, app, max_bytes=6 * 1024 * 1024):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/api/'):
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        try:
            length = int(headers.get(b'content-length', b'0'))
            if length < 0:
                raise ValueError()
        except ValueError:
            return await JSONResponse({'detail': 'Invalid Content-Length'}, status_code=400)(scope, receive, send)
        if length > self.max_bytes:
            return await JSONResponse({'detail': 'Request exceeds 6 MB'}, status_code=413)(scope, receive, send)
        chunks, size = [], 0
        try:
            with anyio.fail_after(30):
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    chunk = message.get('body', b'')
                    size += len(chunk)
                    if size > self.max_bytes:
                        return await JSONResponse({'detail': 'Request exceeds 6 MB'}, status_code=413)(scope, receive, send)
                    chunks.append(chunk)
                    if not message.get('more_body', False):
                        break
        except TimeoutError:
            return await JSONResponse({'detail': 'Request body timed out'}, status_code=408)(scope, receive, send)
        body = b''.join(chunks)
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()
        await self.app(scope, bounded_receive, send)
