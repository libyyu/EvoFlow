import asyncio
import html
import json
import logging
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from evoflow.authz.http_guard import require_org_admin
from evoflow.config.extensions_config import reload_extensions_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["mcp"])


class McpOAuthConfigResponse(BaseModel):
    """OAuth configuration for an MCP server."""

    enabled: bool = Field(default=True, description="Whether OAuth token injection is enabled")
    token_url: str = Field(default="", description="OAuth token endpoint URL")
    grant_type: Literal["client_credentials", "refresh_token"] = Field(default="client_credentials", description="OAuth grant type")
    client_id: str | None = Field(default=None, description="OAuth client ID")
    client_secret: str | None = Field(default=None, description="OAuth client secret")
    refresh_token: str | None = Field(default=None, description="OAuth refresh token")
    scope: str | None = Field(default=None, description="OAuth scope")
    audience: str | None = Field(default=None, description="OAuth audience")
    token_field: str = Field(default="access_token", description="Token response field containing access token")
    token_type_field: str = Field(default="token_type", description="Token response field containing token type")
    expires_in_field: str = Field(default="expires_in", description="Token response field containing expires-in seconds")
    default_token_type: str = Field(default="Bearer", description="Default token type when response omits token_type")
    refresh_skew_seconds: int = Field(default=60, description="Refresh this many seconds before expiry")
    extra_token_params: dict[str, str] = Field(default_factory=dict, description="Additional form params sent to token endpoint")


class McpServerConfigResponse(BaseModel):
    """Response model for MCP server configuration."""

    enabled: bool = Field(default=True, description="Whether this MCP server is enabled")
    type: str = Field(default="stdio", description="Transport type: 'stdio', 'sse', or 'http'")
    command: str | None = Field(default=None, description="Command to execute to start the MCP server (for stdio type)")
    args: list[str] = Field(default_factory=list, description="Arguments to pass to the command (for stdio type)")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables for the MCP server")
    url: str | None = Field(default=None, description="URL of the MCP server (for sse or http type)")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP headers to send (for sse or http type)")
    oauth: McpOAuthConfigResponse | None = Field(default=None, description="OAuth configuration for MCP HTTP/SSE servers")
    description: str = Field(default="", description="Human-readable description of what this MCP server provides")


class McpToolInfo(BaseModel):
    name: str
    full_name: str = ""
    description: str = ""


class McpServerRuntimeStatus(BaseModel):
    """Per-server MCP load/runtime status."""

    load_status: Literal["disabled", "pending", "ready", "error"] = "pending"
    error: str | None = None
    tool_count: int = 0
    tools: list[McpToolInfo] = Field(default_factory=list)


class McpConfigResponse(BaseModel):
    """Response model for MCP configuration."""

    mcp_servers: dict[str, McpServerConfigResponse] = Field(
        default_factory=dict,
        description="Map of MCP server name to configuration",
    )
    tool_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Map of MCP server name to loaded tool count",
    )
    server_status: dict[str, McpServerRuntimeStatus] = Field(
        default_factory=dict,
        description="Per-server runtime load status (green/red indicator)",
    )
    cache_initialized: bool = False
    config_stale: bool = False
    init_error: str | None = None
    total_tools: int = 0


class McpConfigUpdateRequest(BaseModel):
    """Request model for updating MCP configuration."""

    mcp_servers: dict[str, McpServerConfigResponse] = Field(
        ...,
        description="Map of MCP server name to configuration",
    )


class McpMarketServerItem(BaseModel):
    slug: str = ""
    name: str = ""
    title: str = ""
    description: str = ""
    author: str = ""
    url: str = ""
    repository_url: str = ""
    glama_namespace: str = ""
    glama_slug: str = ""
    glama_id: str = ""
    registry_name: str = ""
    source: str = "glama"
    install_cmd: str = ""
    stars: int = 0


class McpMarketSearchResponse(BaseModel):
    servers: list[McpMarketServerItem] = Field(default_factory=list)
    cursor: str | None = None
    has_more: bool = False
    source: str = "glama"
    warning: str | None = None


class McpMarketInstallResponse(BaseModel):
    name: str
    registry_name: str | None = None
    config: McpServerConfigResponse


class McpProjectServerInfo(BaseModel):
    """Workspace（项目）级 MCP server 及其信任门状态。"""

    name: str
    status: Literal["trusted", "changed", "untrusted"] = "untrusted"
    source: str = ""
    enabled: bool = True
    config: dict = Field(default_factory=dict, description="Raw server config from the project mcp.json")


class McpProjectScopeResponse(BaseModel):
    project_root: str
    servers: list[McpProjectServerInfo] = Field(default_factory=list)


class McpProjectTrustRequest(BaseModel):
    name: str = Field(..., description="Workspace MCP server name")
    trusted: bool = Field(..., description="True to trust (fingerprint-bound), False to revoke")


@router.get(
    "/mcp/config",
    response_model=McpConfigResponse,
    summary="Get MCP Configuration",
    description="Retrieve the current Model Context Protocol (MCP) server configurations.",
)
async def get_mcp_configuration(request: Request) -> McpConfigResponse:
    """Get the current MCP configuration and per-server runtime status."""
    require_org_admin(request)
    from evoflow.config.extensions_config import ExtensionsConfig, reload_extensions_config
    from evoflow.mcp.status import build_mcp_status_snapshot

    # 页面每 4 秒轮询这里，顺带续期即将过期的 OAuth 用户令牌。
    # 必须 fire-and-forget：令牌端点最长 15 秒超时，同步 await 会把状态接口
    # 拖住，整个连接器页面会跟着卡；失败也绝不能影响状态读取。
    try:
        from evoflow.mcp import oauth_flow

        async def _oauth_refresh_sweep() -> None:
            try:
                await oauth_flow.refresh_all_expiring()
            except Exception as exc:
                logger.debug("MCP OAuth refresh sweep failed: %s", exc)

        asyncio.create_task(_oauth_refresh_sweep())
    except Exception as exc:
        logger.debug("MCP OAuth refresh sweep skipped: %s", exc)

    try:
        # May import ~/.cursor/mcp.json into SQLite when DB is empty or corrupt
        snap = await asyncio.to_thread(build_mcp_status_snapshot)
    except Exception as exc:
        logger.warning("MCP status snapshot failed; returning degraded response: %s", exc, exc_info=True)
        snap = {
            "servers": [],
            "tool_counts": {},
            "cache_initialized": False,
            "config_stale": False,
            "init_error": f"Failed to build MCP status snapshot: {exc}",
            "total_tools": 0,
        }

    try:
        config = reload_extensions_config()
    except Exception as exc:
        logger.warning("MCP extensions reload failed; returning empty servers: %s", exc, exc_info=True)
        config = ExtensionsConfig()

    server_status = {
        s["name"]: McpServerRuntimeStatus(
            load_status=s["load_status"],
            error=s.get("error"),
            tool_count=int(s.get("tool_count") or 0),
            tools=[
                McpToolInfo(
                    name=str(t.get("name") or ""),
                    full_name=str(t.get("full_name") or ""),
                    description=str(t.get("description") or ""),
                )
                for t in (s.get("tools") or [])
            ],
        )
        for s in snap.get("servers") or []
    }

    return McpConfigResponse(
        mcp_servers={name: McpServerConfigResponse(**server.model_dump()) for name, server in config.mcp_servers.items()},
        tool_counts=snap.get("tool_counts") or {},
        server_status=server_status,
        cache_initialized=bool(snap.get("cache_initialized")),
        config_stale=bool(snap.get("config_stale")),
        init_error=snap.get("init_error"),
        total_tools=int(snap.get("total_tools") or 0),
    )


@router.put(
    "/mcp/config",
    response_model=McpConfigResponse,
    summary="Update MCP Configuration",
    description="Update Model Context Protocol (MCP) server configurations and save to file.",
)
async def update_mcp_configuration(http_request: Request, request: McpConfigUpdateRequest) -> McpConfigResponse:
    """Update the MCP configuration.

    This will:
    1. Save the new configuration to the mcp_config.json file
    2. Reload the configuration cache
    3. Reset MCP tools cache to trigger reinitialization

    Args:
        request: The new MCP configuration to save.

    Returns:
        The updated MCP configuration.

    Raises:
        HTTPException: 500 if the configuration file cannot be written.

    Example Request:
        ```json
        {
            "mcp_servers": {
                "github": {
                    "enabled": true,
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": "$GITHUB_TOKEN"},
                    "description": "GitHub MCP server for repository operations"
                }
            }
        }
        ```
    """
    require_org_admin(http_request)
    try:
        from evoflow.mcp.cache import reset_mcp_tools_cache
        from evoflow.mcp.config_io import unwrap_mcp_servers_dict
        from evoflow.persistence import config_repositories as cfg_repo

        raw = {name: server.model_dump() for name, server in request.mcp_servers.items()}
        # UI may paste full IDE mcp.json: {"mcpServers": {...}}
        unwrapped = unwrap_mcp_servers_dict(raw)
        if not unwrapped and raw:
            unwrapped = raw

        cfg_repo.replace_mcp_servers(unwrapped)
        logger.info("MCP configuration updated in SQLite (%d servers)", len(unwrapped))
        reset_mcp_tools_cache()
        reload_extensions_config()
        return await get_mcp_configuration(http_request)

    except Exception as e:
        logger.error(f"Failed to update MCP configuration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update MCP configuration: {str(e)}")


@router.get(
    "/mcp/project",
    response_model=McpProjectScopeResponse,
    summary="List workspace (project) MCP servers with trust status",
    description=(
        "Project-scope servers come from <project>/.evoflow/mcp.json or .mcp.json and are "
        "untrusted by default — they never connect until explicitly trusted (fingerprint-bound; "
        "editing the config invalidates the trust)."
    ),
)
async def get_mcp_project_scope(request: Request) -> McpProjectScopeResponse:
    require_org_admin(request)
    from evoflow.mcp import workspace_scope

    servers = await asyncio.to_thread(workspace_scope.project_servers_with_status)
    return McpProjectScopeResponse(
        project_root=str(workspace_scope.resolve_project_root()),
        servers=[
            McpProjectServerInfo(
                name=name,
                status=info.get("status") or "untrusted",
                source=str(info.get("source") or ""),
                enabled=bool(info.get("enabled", True)),
                config=info.get("config") or {},
            )
            for name, info in servers.items()
        ],
    )


@router.post(
    "/mcp/project/trust",
    response_model=McpProjectScopeResponse,
    summary="Trust or revoke a workspace MCP server",
    description=(
        "Trust binds the current config fingerprint — a later config change downgrades the "
        "server back to 'changed' (untrusted). Resetting the MCP cache triggers reconnection."
    ),
)
async def trust_mcp_project_server(http_request: Request, request: McpProjectTrustRequest) -> McpProjectScopeResponse:
    require_org_admin(http_request)
    from evoflow.mcp import workspace_scope

    try:
        await asyncio.to_thread(workspace_scope.set_project_server_trust, request.name, request.trusted)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as e:
        logger.error("Failed to update workspace MCP trust for %r: %s", request.name, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update trust: {e}") from e

    try:
        from evoflow.mcp.cache import reset_mcp_tools_cache

        reset_mcp_tools_cache()
    except Exception:
        logger.debug("MCP cache reset after workspace trust change skipped", exc_info=True)

    servers = await asyncio.to_thread(workspace_scope.project_servers_with_status)
    return McpProjectScopeResponse(
        project_root=str(workspace_scope.resolve_project_root()),
        servers=[
            McpProjectServerInfo(
                name=name,
                status=info.get("status") or "untrusted",
                source=str(info.get("source") or ""),
                enabled=bool(info.get("enabled", True)),
                config=info.get("config") or {},
            )
            for name, info in servers.items()
        ],
    )


@router.get(
    "/mcp/market/search",
    response_model=McpMarketSearchResponse,
    summary="Search MCP marketplace (Glama + curated)",
    description="Search the public Glama MCP directory. Empty query returns curated hot servers plus Glama recommendations.",
)
async def search_mcp_marketplace(
    query: str = "",
    cursor: str | None = None,
    limit: int = 20,
) -> McpMarketSearchResponse:
    from evoflow.mcp.market import search_mcp_market

    try:
        data = await search_mcp_market(query=query, cursor=cursor, limit=max(1, min(limit, 50)))
        return McpMarketSearchResponse(**data)
    except Exception as e:
        logger.error("MCP market search failed: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail=f"MCP market search failed: {e}") from e


@router.get(
    "/mcp/market/install",
    response_model=McpMarketInstallResponse,
    summary="Resolve MCP install config from Registry",
    description="Resolve one-click MCP configuration from official Registry (via registry_name or Glama metadata).",
)
async def resolve_mcp_market_install(
    registry_name: str | None = None,
    glama_namespace: str | None = None,
    glama_slug: str | None = None,
    slug: str | None = None,
    repository_url: str | None = None,
) -> McpMarketInstallResponse:
    from evoflow.mcp.market import resolve_mcp_install_config

    try:
        data = await resolve_mcp_install_config(
            registry_name=registry_name,
            glama_namespace=glama_namespace,
            glama_slug=glama_slug,
            slug=slug,
            repository_url=repository_url,
        )
        return McpMarketInstallResponse(
            name=data["name"],
            registry_name=data.get("registry_name"),
            config=McpServerConfigResponse(**data["config"]),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error("MCP market install resolve failed: %s", e, exc_info=True)
        detail = str(e).strip() or repr(e)
        raise HTTPException(status_code=502, detail=f"Failed to resolve MCP install config: {detail}") from e


# ---------------------------------------------------------------------------
# MCP OAuth 2.1（授权码 + PKCE）——协议与状态在 evoflow.mcp.oauth_flow
# 回调是浏览器普通 GET，不带登录态，故此处不挂 require_org_admin。
# ---------------------------------------------------------------------------

class McpOAuthStartRequest(BaseModel):
    name: str = Field(..., description="连接器名称")


_OAUTH_CALLBACK_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>EvoFlow · MCP 授权</title>
<style>
body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:90vh;color:#303133;background:#f7f8fa}
.box{text-align:center;max-width:460px;padding:28px;border-radius:14px;background:#fff;box-shadow:0 12px 40px rgba(0,0,0,.08)}
h3{color:__COLOR__;margin:0 0 12px}p{color:#606266;line-height:1.7}
</style></head><body><div class="box"><h3>__TITLE__</h3><p>__DETAIL__</p><p style="font-size:12px;color:#909399">__HINT__</p></div>
<script>
(function(){
  var data = __PAYLOAD__;
  try { if (window.opener) window.opener.postMessage(data, '*'); } catch(e) {}
  __CLOSE__
})();
</script></body></html>"""


def _oauth_callback_page(ok: bool, title: str, detail: str, name: str = '') -> HTMLResponse:
    payload = json.dumps(
        {'type': 'evoflow-mcp-oauth', 'ok': ok, 'name': name, 'message': detail},
        ensure_ascii=False,
    ).replace('<', '\\u003c')
    page = (
        _OAUTH_CALLBACK_PAGE
        .replace('__COLOR__', '#16a34a' if ok else '#dc2626')
        .replace('__TITLE__', html.escape(title))
        .replace('__DETAIL__', html.escape(detail))
        .replace('__HINT__', html.escape('可以关闭此窗口，回到 EvoFlow 的连接器页面' if ok else '请回到 EvoFlow 重新点「连接」'))
        .replace('__PAYLOAD__', payload)
        .replace('__CLOSE__', 'setTimeout(function(){ window.close(); }, 1200);' if ok else '')
    )
    return HTMLResponse(
        page,
        status_code=200 if ok else 400,
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'},
    )


def _oauth_callback_base(request: Request) -> str:
    """浏览器实际能访问的 Gateway origin，用作 OAuth redirect_uri 前缀。

    桌面端直连 loopback 时 Host 头就够；云服在反向代理后面时 Host 会是内部
    服务名（如 gateway:8001），拿它当 redirect_uri 授权服务器回调不到。

    优先级：
      1. EVOFLOW_PUBLIC_BASE_URL（显式配置，最可靠）
      2. X-Forwarded-Proto / X-Forwarded-Host（需 EVOFLOW_TRUST_PROXY_HEADERS=1，
         只在受信任反代后面开启；nginx 会用 proxy_set_header 覆盖客户端传的值）
      3. request.url.scheme + Host（桌面 loopback / 直连）
    """
    explicit = os.environ.get('EVOFLOW_PUBLIC_BASE_URL', '').strip().rstrip('/')
    if explicit:
        return explicit

    trust_proxy = os.environ.get('EVOFLOW_TRUST_PROXY_HEADERS', '').strip().lower() in (
        '1', 'true', 'yes', 'on',
    )
    if trust_proxy:
        proto = (request.headers.get('x-forwarded-proto') or '').split(',')[0].strip()
        fwd_host = (request.headers.get('x-forwarded-host') or '').split(',')[0].strip()
        if proto in ('http', 'https') and fwd_host:
            return f'{proto}://{fwd_host}'

    host = request.headers.get('host') or '127.0.0.1:8382'
    return f'{request.url.scheme}://{host}'


@router.post('/mcp/oauth/start', summary='发起 MCP OAuth 连接')
async def start_mcp_oauth(request: Request, body: McpOAuthStartRequest):
    require_org_admin(request)
    from evoflow.mcp import oauth_flow
    from evoflow.persistence import config_repositories as cfg_repo

    servers = await asyncio.to_thread(cfg_repo.list_mcp_servers)
    cfg = servers.get(body.name) if isinstance(servers.get(body.name), dict) else {}
    url = str(cfg.get('url') or '').strip()
    if not url:
        raise HTTPException(status_code=400, detail='该连接器没有 URL，只有 http/sse 类型支持 OAuth 授权')
    callback_base = _oauth_callback_base(request)
    try:
        authorize_url = await oauth_flow.start(body.name, url, callback_base)
    except oauth_flow.OAuthFlowError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from None
    return {
        'authorize_url': authorize_url,
        'redirect_uri': callback_base.rstrip('/') + oauth_flow.CALLBACK_PATH,
    }


@router.get('/mcp/oauth/callback', include_in_schema=False, summary='MCP OAuth 授权回调')
async def mcp_oauth_callback(
    code: str = '',
    state: str = '',
    error: str = '',
    error_description: str = '',
):
    from evoflow.mcp import oauth_flow

    if error:
        reason = error_description or error
        return _oauth_callback_page(False, '授权没有完成', '你取消了授权' if error == 'access_denied' else f'授权服务器返回：{reason}')
    if not code or not state:
        return _oauth_callback_page(False, '回调缺少参数', '请回到 EvoFlow 重新点「连接」')
    try:
        name = await oauth_flow.complete(code, state)
    except oauth_flow.OAuthFlowError as exc:
        return _oauth_callback_page(False, '连接失败', exc.message)
    except Exception as exc:
        logger.error("MCP OAuth callback failed: %s", exc, exc_info=True)
        return _oauth_callback_page(False, '连接失败', str(exc))
    return _oauth_callback_page(True, '连接成功', f'连接器「{name}」已完成授权，工具清单正在刷新', name)


@router.get('/mcp/oauth/status', summary='查询 MCP OAuth 连接状态')
async def mcp_oauth_status(request: Request, name: str = ''):
    require_org_admin(request)
    from evoflow.mcp import oauth_flow

    return oauth_flow.connection_status(name)


@router.delete('/mcp/oauth', summary='断开 MCP OAuth 连接')
async def disconnect_mcp_oauth(request: Request, name: str = ''):
    require_org_admin(request)
    from evoflow.mcp import oauth_flow

    try:
        await asyncio.to_thread(oauth_flow.disconnect, name)
    except oauth_flow.OAuthFlowError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from None
    return {'ok': True}
