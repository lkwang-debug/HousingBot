"""Daily bearer links. Standard library only; shared with the cloud function."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
AUDIENCE = 'housing-lookup'


class AccessError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _key(secret):
    if not isinstance(secret, str) or len(secret.encode('utf-8')) < 32:
        raise ValueError('查询签名密钥未配置或不足32字节')
    return secret.encode('utf-8')


def _encode(raw):
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _decode(raw):
    if not raw or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_' for c in raw):
        raise ValueError('invalid base64url')
    decoded = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
    if _encode(decoded) != raw:
        raise ValueError('noncanonical encoding')
    return decoded


def beijing_now(now=None):
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=BEIJING) if value.tzinfo is None else value.astimezone(BEIJING)


def issue_token(secret, now=None):
    start = beijing_now(now).replace(hour=0, minute=0, second=0, microsecond=0)
    claims = {'v': 1, 'aud': AUDIENCE, 'nbf': int(start.timestamp()),
              'exp': int((start + timedelta(days=1)).timestamp())}
    payload = _encode(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode())
    signature = _encode(hmac.new(_key(secret), ('v1.' + payload).encode(), hashlib.sha256).digest())
    return 'v1.' + payload + '.' + signature


def verify_token(token, secret, now=None):
    key = _key(secret)
    try:
        if not isinstance(token, str) or len(token) > 1024:
            raise ValueError('bad token')
        version, payload, signature = token.split('.')
        expected = hmac.new(key, (version + '.' + payload).encode(), hashlib.sha256).digest()
        if version != 'v1' or not hmac.compare_digest(expected, _decode(signature)):
            raise ValueError('bad signature')
        claims = json.loads(_decode(payload))
        if set(claims) != {'v', 'aud', 'nbf', 'exp'} or type(claims['v']) is not int or claims['v'] != 1 or claims['aud'] != AUDIENCE:
            raise ValueError('bad claims')
        if type(claims['nbf']) is not int or type(claims['exp']) is not int:
            raise ValueError('bad dates')
        start = datetime.fromtimestamp(claims['nbf'], BEIJING)
        if start.hour or start.minute or start.second or claims['exp'] - claims['nbf'] != 86400:
            raise ValueError('not a Beijing calendar day')
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        raise AccessError('invalid_token', '链接无效，请使用今天推送中的查询链接。') from None
    stamp = beijing_now(now).timestamp()
    if stamp < claims['nbf']:
        raise AccessError('not_active', '本链接尚未生效，请使用今天推送中的查询链接。')
    if stamp >= claims['exp']:
        raise AccessError('expired', '本链接已过期，请使用今天推送中的查询链接。')
    return claims
