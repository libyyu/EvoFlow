"""MCP OAuth 2.1 授权码客户端（EvoFlow 桌面单机版）。

借鉴 RuoYi-Vue3-FastAPI 的 ``plugins/flow/mcp/oauth.py``（已在
``srag.mcp.woa.com`` / ``iam.it.woa.com`` 实测走通），按单机桌面场景简化：

- 协议：401 探测 → RFC 9728 受保护资源元数据 → RFC 8414/OIDC 授权服务器元数据
  → RFC 7591 动态注册公开客户端 → 授权码 + PKCE(S256) + RFC 8707 resource
  → 换令牌 / refresh_token 续期。
- 单用户：state 与令牌都落共享数据目录（每 state 一个 JSON 文件，10 分钟 TTL），
  因此 start / callback 可以落在不同 uvicorn worker；不需要 Redis / 多用户身份 /
  加密 state。跨进程 refresh 仍需单 worker 或分布式锁（见 docker-compose.yaml）。
- 注入：拿到令牌后直接写进 SQLite 里该 server 的 ``headers.Authorization``，
  复用 EvoFlow 已有的 headers → langchain-mcp-adapters 链路，不改 MCP 加载代码。

仅处理协议与本机状态；HTTP 路由在 ``app.gateway.routers.mcp``。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from starlette import status as http_status

logger = logging.getLogger(__name__)

CLIENT_NAME = 'EvoFlow Desktop'
DISCOVERY_TIMEOUT = 15
STATE_TTL_SECONDS = 600
REFRESH_MARGIN_SECONDS = 300
CALLBACK_PATH = '/api/mcp/oauth/callback'
_PROBE_PROTOCOL = '2025-06-18'

# 云服 uvicorn 可开多 worker：start 与 callback 可能落不同进程。
# state 改为「每 state 一个 JSON 文件」放在共享数据目录，而不是进程内存；
# 令牌文件本来就在同一共享卷上。跨进程 refresh 锁仍由部署侧单 worker 约束保证。
_REFRESH_LOCK = asyncio.Lock()


class OAuthFlowError(Exception):
    """授权流程失败（消息可直接展示给用户）。"""

    def __init__(self, message: str, *, revoked: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.revoked = revoked


@dataclass
class OAuthServer:
    """自动发现到的授权信息（不含密钥，可落盘）。"""

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str = ''
    scopes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'OAuthServer':
        return cls(
            resource=str(data.get('resource') or ''),
            issuer=str(data.get('issuer') or ''),
            authorization_endpoint=str(data.get('authorization_endpoint') or ''),
            token_endpoint=str(data.get('token_endpoint') or ''),
            registration_endpoint=str(data.get('registration_endpoint') or ''),
            scopes=[str(item) for item in data.get('scopes') or []],
        )


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scope: str


# ---------------------------------------------------------------- 协议层

def _client() -> httpx.AsyncClient:
    # trust_env=True（默认）让内网代理环境变量生效
    return httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT, follow_redirects=False)


async def _request(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise OAuthFlowError(f'不允许的端点地址：{url}')
    try:
        return await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise OAuthFlowError(f'请求 {parts.netloc} 失败：{exc}') from None


def _json(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise OAuthFlowError(f'{what}不是 JSON（HTTP {response.status_code}）') from None
    if not isinstance(data, dict):
        raise OAuthFlowError(f'{what}格式不对')
    return data


def _challenge_params(header: str) -> dict[str, str]:
    """``Bearer resource_metadata="x", scope="a b"`` → 参数字典。"""
    _, _, rest = header.partition(' ')
    params: dict[str, str] = {}
    for part in rest.split(','):
        key, sep, value = part.strip().partition('=')
        if sep:
            params[key.strip().lower()] = value.strip().strip('"')
    return params


def _well_known(base: str, name: str) -> list[str]:
    """RFC 8414 / 9728：路径插入式优先，其次根路径与后缀式。"""
    parts = urlsplit(base)
    origin = f'{parts.scheme}://{parts.netloc}'
    path = parts.path.rstrip('/')
    candidates = [f'{origin}/.well-known/{name}{path}'] if path else []
    candidates.append(f'{origin}/.well-known/{name}')
    if path:
        candidates.append(f'{origin}{path}/.well-known/{name}')
    return list(dict.fromkeys(candidates))


async def probe(url: str) -> OAuthServer | None:
    """不带令牌 initialize 一次：要求 OAuth 返回发现结果，否则返回 None。"""
    body = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'initialize',
        'params': {
            'protocolVersion': _PROBE_PROTOCOL,
            'capabilities': {},
            'clientInfo': {'name': 'probe', 'version': '0'},
        },
    }
    async with _client() as client:
        response = await _request(
            client, 'POST', url, json=body,
            headers={'Accept': 'application/json, text/event-stream'},
        )
        if response.status_code != http_status.HTTP_401_UNAUTHORIZED:
            return None
        header = response.headers.get('www-authenticate', '')
        if not header.lower().startswith('bearer'):
            return None
        return await _discover(client, url, _challenge_params(header))


async def _discover(
    client: httpx.AsyncClient, url: str, challenge: dict[str, str]
) -> OAuthServer:
    resource_meta: dict[str, Any] | None = None
    candidates = (
        [challenge['resource_metadata']]
        if challenge.get('resource_metadata')
        else _well_known(url, 'oauth-protected-resource')
    )
    for candidate in candidates:
        response = await _request(client, 'GET', candidate)
        if response.status_code == http_status.HTTP_200_OK:
            try:
                resource_meta = _json(response, '受保护资源元数据')
                break
            except OAuthFlowError:
                # SPA 兜底 HTML 会被当成 200，跳过继续找
                continue
    if resource_meta is None:
        raise OAuthFlowError('服务器要求 OAuth，但找不到受保护资源元数据（RFC 9728）')
    issuers = resource_meta.get('authorization_servers') or []
    if not issuers:
        raise OAuthFlowError('受保护资源元数据里没有授权服务器地址')
    issuer = str(issuers[0])

    server_meta: dict[str, Any] | None = None
    for candidate in [
        *_well_known(issuer, 'oauth-authorization-server'),
        *_well_known(issuer, 'openid-configuration'),
    ]:
        response = await _request(client, 'GET', candidate)
        if response.status_code == http_status.HTTP_200_OK:
            try:
                server_meta = _json(response, '授权服务器元数据')
            except OAuthFlowError:
                continue
            if server_meta.get('authorization_endpoint') and server_meta.get('token_endpoint'):
                break
            server_meta = None
    if server_meta is None:
        raise OAuthFlowError(f'找不到授权服务器 {issuer} 的元数据（RFC 8414）')
    methods = server_meta.get('code_challenge_methods_supported')
    if methods is not None and 'S256' not in methods:
        raise OAuthFlowError('授权服务器不支持 PKCE S256，拒绝接入')

    scope_text = challenge.get('scope') or ''
    scopes = scope_text.split() or [
        str(item)
        for item in resource_meta.get('scopes_supported')
        or resource_meta.get('resource_scopes')
        or []
    ]
    return OAuthServer(
        resource=str(resource_meta.get('resource') or url),
        issuer=str(server_meta.get('issuer') or issuer),
        authorization_endpoint=str(server_meta['authorization_endpoint']),
        token_endpoint=str(server_meta['token_endpoint']),
        registration_endpoint=str(server_meta.get('registration_endpoint') or ''),
        scopes=scopes,
    )


async def register(server: OAuthServer, redirect_uri: str) -> str:
    """动态注册公开客户端（RFC 7591），返回 client_id。"""
    if not server.registration_endpoint:
        raise OAuthFlowError('授权服务器不支持动态注册客户端，无法自动接入')
    payload = {
        'client_name': CLIENT_NAME,
        'redirect_uris': [redirect_uri],
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'token_endpoint_auth_method': 'none',
    }
    async with _client() as client:
        response = await _request(client, 'POST', server.registration_endpoint, json=payload)
    data = _json(response, '注册结果')
    if response.status_code not in (
        http_status.HTTP_200_OK, http_status.HTTP_201_CREATED
    ) or not data.get('client_id'):
        raise OAuthFlowError(
            f'注册客户端失败（HTTP {response.status_code}）：'
            f'{data.get("error_description") or data.get("error") or data}'
        )
    return str(data['client_id'])


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b'=').decode()
    return verifier, challenge


def authorize_url(
    server: OAuthServer, client_id: str, redirect_uri: str, state: str, challenge: str
) -> str:
    query: dict[str, str] = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'resource': server.resource,
    }
    if server.scopes:
        query['scope'] = ' '.join(server.scopes)
    separator = '&' if '?' in server.authorization_endpoint else '?'
    return f'{server.authorization_endpoint}{separator}{urlencode(query)}'


async def _token_request(server: OAuthServer, form: dict[str, str]) -> TokenSet:
    async with _client() as client:
        response = await _request(
            client, 'POST', server.token_endpoint,
            data={**form, 'resource': server.resource},
            headers={'Accept': 'application/json'},
        )
    data = _json(response, '令牌响应')
    if response.status_code != http_status.HTTP_200_OK or not data.get('access_token'):
        error = str(data.get('error') or '')
        detail = data.get('error_description') or error or f'HTTP {response.status_code}'
        raise OAuthFlowError(f'换取令牌失败：{detail}', revoked=error == 'invalid_grant')
    if str(data.get('token_type') or 'bearer').lower() != 'bearer':
        raise OAuthFlowError(f'不支持的令牌类型：{data.get("token_type")}')
    expires = data.get('expires_in')
    return TokenSet(
        access_token=str(data['access_token']),
        refresh_token=str(data['refresh_token']) if data.get('refresh_token') else None,
        expires_in=int(expires) if str(expires or '').isdigit() else None,
        scope=str(data.get('scope') or ' '.join(server.scopes)),
    )


async def exchange_code(
    server: OAuthServer, client_id: str, redirect_uri: str, code: str, verifier: str
) -> TokenSet:
    return await _token_request(server, {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'code_verifier': verifier,
    })


async def refresh(server: OAuthServer, client_id: str, refresh_token: str) -> TokenSet:
    return await _token_request(server, {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': client_id,
    })


# ---------------------------------------------------------------- 本机状态

def _data_dir() -> Path:
    """Directory holding OAuth tokens and in-flight state files.

    Desktop history: tokens were always written to ``~/.evoflow/data`` and
    EVOFLOW_HOME was ignored. Keep that path when an existing token file is
    present so an in-place upgrade does not invalidate already-authorized MCP
    connectors.

    Cloud: compose mounts EVOFLOW_HOME as a shared volume. Use the project's
    canonical resolver so tokens survive container restarts and are visible to
    every uvicorn worker (it also handles the legacy ``.../workspace/data``
    double-data layout).
    """
    legacy = Path.home() / '.evoflow' / 'data'
    if (legacy / 'mcp_oauth_tokens.json').exists():
        return legacy
    try:
        from evoflow.config.data_paths import resolve_data_root

        return Path(resolve_data_root())
    except Exception:
        logger.warning('MCP OAuth data dir resolver failed, using fallback', exc_info=True)
        root = os.environ.get('EVOFLOW_HOME', '').strip()
        base = Path(root) if root else Path.home() / '.evoflow'
        return base / 'data'


def _token_path() -> Path:
    return _data_dir() / 'mcp_oauth_tokens.json'


def _state_dir() -> Path:
    return _data_dir() / 'mcp_oauth_states'


def _state_path(state: str) -> Path:
    # state comes from secrets.token_urlsafe -> [A-Za-z0-9_-]; sanitize anyway
    # so a crafted callback value can never escape the state directory.
    safe = ''.join(ch for ch in state if ch.isalnum() or ch in ('-', '_'))[:128]
    return _state_dir() / f'{safe}.json'


def _save_state(state: str, entry: dict[str, Any]) -> None:
    directory = _state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = _state_path(state)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def _take_state(state: str) -> dict[str, Any] | None:
    """Atomically consume a state file so two workers cannot exchange one code twice.

    os.replace() is atomic on the same filesystem; the loser sees no file and
    gets `None`, which surfaces as a normal "state invalid/expired" error.
    """
    path = _state_path(state)
    if not path.is_file():
        return None
    consumed = path.with_suffix('.json.consumed')
    try:
        os.replace(path, consumed)
    except OSError:
        return None
    try:
        data = json.loads(consumed.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        try:
            consumed.unlink(missing_ok=True)
        except OSError:
            pass


def _load_tokens() -> dict[str, Any]:
    path = _token_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        logger.warning('MCP OAuth token store unreadable, starting empty', exc_info=True)
        return {}


def _save_tokens(tokens: dict[str, Any]) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _update_server_headers(name: str, authorization: str | None) -> None:
    """把用户令牌写进（或从）该 server 的 headers.Authorization，并触发重新加载。"""
    from evoflow.persistence import config_repositories as cfg_repo

    servers = cfg_repo.list_mcp_servers()
    current = servers.get(name)
    if not isinstance(current, dict):
        raise OAuthFlowError(f'连接器「{name}」不存在')
    headers = dict(current.get('headers') or {})
    for key in list(headers):
        if key.lower() == 'authorization':
            del headers[key]
    if authorization:
        headers['Authorization'] = authorization
    current['headers'] = headers
    servers[name] = current
    cfg_repo.replace_mcp_servers(servers)
    try:
        from evoflow.mcp.cache import reset_mcp_tools_cache
        from evoflow.config.extensions_config import reload_extensions_config

        reset_mcp_tools_cache()
        reload_extensions_config()
    except Exception:
        logger.warning('MCP reload after OAuth token update failed', exc_info=True)


def _purge_expired_states() -> None:
    directory = _state_dir()
    if not directory.is_dir():
        return
    now = time.time()
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            if path.suffix == '.json':
                data = json.loads(path.read_text(encoding='utf-8'))
                expired = float(data.get('exp') or 0) < now
            else:
                # .tmp / .consumed leftovers: expire by mtime instead.
                expired = now - path.stat().st_mtime > STATE_TTL_SECONDS * 2
        except (OSError, ValueError):
            expired = True
        if expired:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------- 对外流程

async def start(name: str, url: str, callback_base: str) -> str:
    """发起连接：探测 → 动态注册 → 返回授权地址。"""
    server = await probe(url)
    if server is None:
        raise OAuthFlowError('该地址未声明 OAuth 授权（401 无 Bearer 质询）。静态 Bearer key 请用「请求头 Token」。')
    redirect_uri = callback_base.rstrip('/') + CALLBACK_PATH

    tokens = _load_tokens()
    record = tokens.get(name) if isinstance(tokens.get(name), dict) else {}
    client_id = str(record.get('client_id') or '') if record.get('redirect_uri') == redirect_uri else ''
    if not client_id:
        client_id = await register(server, redirect_uri)

    verifier, challenge = pkce_pair()
    _purge_expired_states()
    state = secrets.token_urlsafe(24)
    _save_state(state, {
        'name': name,
        'verifier': verifier,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'server': server.to_dict(),
        'exp': time.time() + STATE_TTL_SECONDS,
    })
    return authorize_url(server, client_id, redirect_uri, state, challenge)


async def complete(code: str, state: str) -> str:
    """回调：校验 state → 换令牌 → 落盘 → 写进连接器 headers。"""
    entry = _take_state(state)
    if not entry or float(entry.get('exp') or 0) < time.time():
        raise OAuthFlowError('授权状态无效或已超时，请重新点「连接」')
    server = OAuthServer.from_dict(entry['server'])
    tokenset = await exchange_code(
        server, str(entry['client_id']), str(entry['redirect_uri']), code, str(entry['verifier'])
    )
    name = str(entry['name'])
    records = _load_tokens()
    records[name] = {
        'access_token': tokenset.access_token,
        'refresh_token': tokenset.refresh_token,
        'expires_at': time.time() + (tokenset.expires_in or 0) if tokenset.expires_in else None,
        'client_id': entry['client_id'],
        'scope': tokenset.scope,
        'redirect_uri': entry['redirect_uri'],
        'server': entry['server'],
    }
    _save_tokens(records)
    _update_server_headers(name, f'Bearer {tokenset.access_token}')
    return name


def connection_status(name: str) -> dict[str, Any]:
    records = _load_tokens()
    record = records.get(name)
    if not isinstance(record, dict):
        return {'connected': False}
    expires_at = record.get('expires_at')
    return {
        'connected': True,
        'expires_at': expires_at,
        'expired': bool(expires_at and float(expires_at) <= time.time()),
        'scope': record.get('scope') or '',
    }


def disconnect(name: str) -> None:
    records = _load_tokens()
    if name in records:
        records.pop(name)
        _save_tokens(records)
    try:
        _update_server_headers(name, None)
    except OAuthFlowError:
        # 连接器可能已被用户删除；令牌已清掉就够了，不要让「断开」报错。
        logger.info('MCP OAuth disconnect: server %s no longer in config', name)


async def refresh_all_expiring() -> None:
    """续期即将过期的令牌；由 GET /api/mcp/config 的轮询以 fire-and-forget 触发。

    页面每 4 秒就会轮询一次 config，所以这里必须非阻塞：上一次续期没结束
    就直接返回，避免堆积 task 或把状态接口的响应拖到 15 秒超时。
    """
    if _REFRESH_LOCK.locked():
        return
    records = _load_tokens()
    if not records:
        return
    async with _REFRESH_LOCK:
        for name, record in list(records.items()):
            if not isinstance(record, dict):
                continue
            expires_at = float(record.get('expires_at') or 0)
            # 授权服务器没给 expires_in 时无法判断何时该续期，别每 4 秒刷一次。
            if not expires_at or expires_at - time.time() > REFRESH_MARGIN_SECONDS:
                continue
            refresh_token = record.get('refresh_token')
            if not refresh_token:
                continue
            server = OAuthServer.from_dict(record.get('server') or {})
            if not server.token_endpoint:
                continue
            try:
                fresh = await refresh(server, str(record.get('client_id') or ''), str(refresh_token))
            except OAuthFlowError as exc:
                if exc.revoked:
                    logger.info('MCP OAuth token revoked for %s, clearing', name)
                    try:
                        disconnect(name)
                    except Exception:
                        logger.warning('Failed to clear revoked token for %s', name, exc_info=True)
                else:
                    logger.warning('MCP OAuth refresh failed for %s: %s', name, exc.message)
                continue
            record['access_token'] = fresh.access_token
            record['refresh_token'] = fresh.refresh_token or refresh_token
            record['expires_at'] = (
                time.time() + fresh.expires_in if fresh.expires_in else None
            )
            record['scope'] = fresh.scope
            _save_tokens(records)
            try:
                _update_server_headers(name, f'Bearer {fresh.access_token}')
            except Exception:
                logger.warning('Failed to write refreshed header for %s', name, exc_info=True)
