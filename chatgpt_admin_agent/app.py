import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
import uvicorn
import yaml
from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field


CONFIG_ROOT = Path("/config").resolve()
SHARE_ROOT = Path("/share").resolve()
BACKUP_ROOT = Path("/backup").resolve()
MEDIA_ROOT = Path("/media").resolve()
SSL_ROOT = Path("/ssl").resolve()

ALLOWED_ROOTS = [
    CONFIG_ROOT,
    SHARE_ROOT,
    BACKUP_ROOT,
    MEDIA_ROOT,
    SSL_ROOT,
]

OPTIONS_PATH = Path("/data/options.json")
AGENT_VERSION = "0.1.8"

SUPERVISOR_URL = "http://supervisor"
SUPERVISOR_CORE_API = "http://supervisor/core/api"


def load_options() -> dict:
    if OPTIONS_PATH.exists():
        return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    return {}


OPTIONS = load_options()

ADMIN_TOKEN = OPTIONS.get("admin_token", "change-me")
ALLOW_SHELL = bool(OPTIONS.get("allow_shell", True))
ALLOW_STORAGE = bool(OPTIONS.get("allow_storage", True))

CONFIGURED_HA_URL = str(
    OPTIONS.get("ha_url")
    or os.environ.get("HA_URL")
    or "http://homeassistant.local:8123"
).rstrip("/")

CONFIGURED_HA_TOKEN = str(
    OPTIONS.get("ha_token")
    or os.environ.get("HA_TOKEN")
    or ""
).strip()

SUPERVISOR_TOKEN = str(
    os.environ.get("SUPERVISOR_TOKEN")
    or os.environ.get("HASSIO_TOKEN")
    or os.environ.get("HOMEASSISTANT_TOKEN")
    or ""
).strip()

app = FastAPI(title="ChatGPT Admin Agent", version=AGENT_VERSION)


def require_auth(x_admin_token: Optional[str], token: Optional[str] = None) -> None:
    if not ADMIN_TOKEN or ADMIN_TOKEN == "change-me":
        raise HTTPException(status_code=403, detail="Admin token not configured")

    if x_admin_token == ADMIN_TOKEN or token == ADMIN_TOKEN:
        return

    raise HTTPException(status_code=401, detail="Invalid admin token")


def resolve_path(relative_path: str) -> Path:
    clean = relative_path.strip()

    if clean.startswith("/"):
        path = Path(clean).resolve()
    else:
        path = (CONFIG_ROOT / clean.lstrip("/")).resolve()

    if not any(path == root or root in path.parents for root in ALLOWED_ROOTS):
        raise HTTPException(status_code=403, detail=f"Path outside allowed roots: {path}")

    if ".storage" in path.parts and not ALLOW_STORAGE:
        raise HTTPException(status_code=403, detail="Storage access disabled")

    return path


def create_backup(path: Path) -> Optional[str]:
    if not path.exists():
        return None

    backup_dir = CONFIG_ROOT / "chatgpt_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = str(path).replace("/", "_").replace("\\", "_").strip("_")
    backup_path = backup_dir / f"{stamp}_{safe_name}.bak"

    shutil.copy2(path, backup_path)
    return str(backup_path)


def ha_api_base_url() -> str:
    if SUPERVISOR_TOKEN:
        return SUPERVISOR_CORE_API

    if CONFIGURED_HA_TOKEN:
        if CONFIGURED_HA_URL.endswith("/api"):
            return CONFIGURED_HA_URL
        return f"{CONFIGURED_HA_URL}/api"

    raise HTTPException(
        status_code=500,
        detail=(
            "No Home Assistant API token available. "
            "Expected SUPERVISOR_TOKEN/HASSIO_TOKEN/HOMEASSISTANT_TOKEN or ha_token in addon options."
        ),
    )


def ha_headers() -> dict:
    token = SUPERVISOR_TOKEN or CONFIGURED_HA_TOKEN

    if not token:
        raise HTTPException(
            status_code=500,
            detail=(
                "No Home Assistant API token available. "
                "Expected SUPERVISOR_TOKEN/HASSIO_TOKEN/HOMEASSISTANT_TOKEN or ha_token in addon options."
            ),
        )

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def ha_get(path: str):
    url = f"{ha_api_base_url()}{path}"

    try:
        response = requests.get(url, headers=ha_headers(), timeout=30)

        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        if response.text:
            try:
                return response.json()
            except Exception:
                return {"ok": True, "text": response.text}

        return {"ok": True}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def ha_post(path: str, payload: Optional[dict] = None, timeout: int = 30):
    url = f"{ha_api_base_url()}{path}"

    try:
        response = requests.post(
            url,
            headers=ha_headers(),
            json=payload or {},
            timeout=timeout,
        )

        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        if response.text:
            try:
                return response.json()
            except Exception:
                return {"ok": True, "text": response.text}

        return {"ok": True}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class FilePathRequest(BaseModel):
    relative_path: str


class WriteFileRequest(BaseModel):
    relative_path: str
    content: str
    backup: bool = True


class ReplaceInFileRequest(BaseModel):
    relative_path: str
    search: str
    replace: str
    backup: bool = True


class GrepRequest(BaseModel):
    relative_path: str
    search: str


class ShellRequest(BaseModel):
    command: str
    cwd: str = "/config"
    timeout: int = 30


class ServiceRequest(BaseModel):
    domain: str
    service: str
    target: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)


class DirectServiceRequest(BaseModel):
    target: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)


class TemplateRequest(BaseModel):
    template: str


class EntityStateRequest(BaseModel):
    entity_id: str


class DeleteRestoreStateRequest(BaseModel):
    entity_id: str
    backup: bool = True


@app.get("/health", operation_id="ha_agent_health")
def health():
    return {
        "ok": True,
        "name": "ChatGPT Admin Agent",
        "version": AGENT_VERSION,
        "supervisor_token_available": bool(SUPERVISOR_TOKEN),
        "configured_ha_token_available": bool(CONFIGURED_HA_TOKEN),
        "configured_ha_url": CONFIGURED_HA_URL,
        "ha_api_base_url": ha_api_base_url() if (SUPERVISOR_TOKEN or CONFIGURED_HA_TOKEN) else None,
    }


@app.post("/fs/read", operation_id="ha_agent_read_file")
def fs_read(
    req: FilePathRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    path = resolve_path(req.relative_path)

    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    return {
        "ok": True,
        "path": str(path),
        "content": path.read_text(encoding="utf-8", errors="replace"),
    }


@app.post("/fs/write", operation_id="ha_agent_write_file")
def fs_write(
    req: WriteFileRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    path = resolve_path(req.relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    backup = create_backup(path) if req.backup else None
    path.write_text(req.content, encoding="utf-8")

    return {
        "ok": True,
        "changed": True,
        "path": str(path),
        "backup": backup,
    }


@app.post("/fs/replace", operation_id="ha_agent_replace_in_file")
def fs_replace(
    req: ReplaceInFileRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    path = resolve_path(req.relative_path)

    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    content = path.read_text(encoding="utf-8", errors="replace")
    count = content.count(req.search)

    if count == 0:
        return {
            "ok": True,
            "changed": False,
            "count": 0,
            "message": "Search text not found",
        }

    backup = create_backup(path) if req.backup else None
    path.write_text(content.replace(req.search, req.replace), encoding="utf-8")

    return {
        "ok": True,
        "changed": True,
        "count": count,
        "backup": backup,
    }


@app.post("/fs/grep", operation_id="ha_agent_grep_file")
def fs_grep(
    req: GrepRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    path = resolve_path(req.relative_path)

    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    matches = []

    for index, line in enumerate(lines, start=1):
        if req.search in line:
            matches.append({"line": index, "text": line})

    return {
        "ok": True,
        "count": len(matches),
        "matches": matches[:300],
    }


@app.post("/yaml/validate", operation_id="ha_agent_validate_yaml")
def yaml_validate(
    req: FilePathRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    path = resolve_path(req.relative_path)

    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"ok": True}


@app.post("/json/validate", operation_id="ha_agent_validate_json")
def json_validate(
    req: FilePathRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    path = resolve_path(req.relative_path)

    try:
        json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"ok": True}


@app.post("/restore_state/delete_entity", operation_id="ha_agent_delete_restore_state_entity")
def restore_state_delete_entity(
    req: DeleteRestoreStateRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    if not ALLOW_STORAGE:
        raise HTTPException(status_code=403, detail="Storage access disabled")

    path = CONFIG_ROOT / ".storage" / "core.restore_state"

    if not path.exists():
        raise HTTPException(status_code=404, detail="core.restore_state not found")

    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("data", [])

    new_items = []
    removed = 0

    for item in items:
        state = item.get("state", {})
        if state.get("entity_id") == req.entity_id:
            removed += 1
            continue
        new_items.append(item)

    if removed == 0:
        return {
            "ok": True,
            "changed": False,
            "removed": 0,
        }

    backup = create_backup(path) if req.backup else None
    data["data"] = new_items
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "changed": True,
        "removed": removed,
        "backup": backup,
    }


@app.get("/ha/states", operation_id="ha_agent_get_states")
def ha_get_states(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return ha_get("/states")


@app.get("/ha/states/{entity_id}", operation_id="ha_agent_get_state")
def ha_get_state_by_path(
    entity_id: str,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return ha_get(f"/states/{entity_id}")


@app.post("/ha/get_state", operation_id="ha_agent_get_state_post")
def ha_get_state(
    req: EntityStateRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return ha_get(f"/states/{req.entity_id}")


@app.get("/ha/services", operation_id="ha_agent_list_services")
def ha_list_services(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return ha_get("/services")


@app.post("/ha/call_service", operation_id="ha_agent_call_service")
def ha_call_service(
    req: ServiceRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    payload: dict[str, Any] = {}
    payload.update(req.data or {})

    if req.target:
        payload["target"] = req.target

    return ha_post(f"/services/{req.domain}/{req.service}", payload)


@app.post("/ha/services/{domain}/{service}", operation_id="ha_agent_call_service_direct")
def ha_call_service_direct(
    domain: str,
    service: str,
    req: DirectServiceRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    payload: dict[str, Any] = {}
    payload.update(req.data or {})

    if req.target:
        payload["target"] = req.target

    return ha_post(f"/services/{domain}/{service}", payload)


@app.post("/ha/template", operation_id="ha_agent_render_template")
def ha_render_template(
    req: TemplateRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    try:
        response = requests.post(
            f"{ha_api_base_url()}/template",
            headers=ha_headers(),
            json={"template": req.template},
            timeout=30,
        )

        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        return {
            "ok": True,
            "result": response.text,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/ha/reload_scripts", operation_id="ha_agent_reload_scripts")
def ha_reload_scripts(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return ha_post("/services/script/reload", {})


@app.post("/ha/reload_automations", operation_id="ha_agent_reload_automations")
def ha_reload_automations(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return ha_post("/services/automation/reload", {})


@app.post("/ha/reload_core_config", operation_id="ha_agent_reload_core_config")
def ha_reload_core_config(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return ha_post("/services/homeassistant/reload_core_config", {})


def run_shell(command: str, cwd: str, timeout: int):
    cwd_path = resolve_path(cwd)

    dangerous_fragments = [
        "rm -rf /",
        "mkfs",
        "dd if=",
        ":(){",
        "shutdown",
        "poweroff",
    ]

    if any(fragment in command for fragment in dangerous_fragments):
        raise HTTPException(status_code=403, detail="Dangerous command blocked")

    result = subprocess.run(
        command,
        shell=True,
        cwd=str(cwd_path),
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout[-20000:],
        "stderr": result.stderr[-20000:],
    }


@app.post("/shell/exec", operation_id="ha_agent_shell_exec")
def shell_exec(
    req: ShellRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    if not ALLOW_SHELL:
        raise HTTPException(status_code=403, detail="Shell access disabled")

    return run_shell(req.command, req.cwd, req.timeout)


@app.post("/ha/check_config", operation_id="ha_agent_check_config")
def ha_check_config(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    errors = []

    if SUPERVISOR_TOKEN:
        headers = {
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        }

        candidates = [
            f"{SUPERVISOR_URL}/core/check_config",
            f"{SUPERVISOR_URL}/core/check",
        ]

        for url in candidates:
            try:
                response = requests.post(url, headers=headers, json={}, timeout=120)

                if response.status_code == 404:
                    errors.append(
                        {
                            "url": url,
                            "status": response.status_code,
                            "body": response.text,
                        }
                    )
                    continue

                if response.status_code >= 400:
                    errors.append(
                        {
                            "url": url,
                            "status": response.status_code,
                            "body": response.text,
                        }
                    )
                    continue

                if response.text:
                    try:
                        return response.json()
                    except Exception:
                        return {
                            "ok": True,
                            "text": response.text,
                        }

                return {"ok": True}

            except Exception as exc:
                errors.append(
                    {
                        "url": url,
                        "error": str(exc),
                    }
                )

    if CONFIGURED_HA_TOKEN:
        try:
            result = ha_post("/services/homeassistant/check_config", {}, timeout=120)
            return {
                "ok": True,
                "method": "homeassistant_service",
                "result": result,
            }
        except Exception as exc:
            errors.append(
                {
                    "method": "homeassistant_service",
                    "error": str(exc),
                }
            )

    if ALLOW_SHELL:
        fallback = run_shell("ha core check", "/config", 120)
        fallback["fallback"] = "shell"
        fallback["supervisor_errors"] = errors
        return fallback

    raise HTTPException(
        status_code=500,
        detail={
            "message": "No supported config-check method worked.",
            "errors": errors,
        },
    )


def mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "ha_agent_health",
            "description": "Check whether the ChatGPT Admin Agent is reachable.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_read_file",
            "description": "Read a Home Assistant file from an allowed path.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                },
                "required": ["relative_path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_write_file",
            "description": "Write a Home Assistant file with optional backup.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "content": {"type": "string"},
                    "backup": {"type": "boolean", "default": True},
                },
                "required": ["relative_path", "content"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_replace_in_file",
            "description": "Replace text in a Home Assistant config file with backup.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "search": {"type": "string"},
                    "replace": {"type": "string"},
                    "backup": {"type": "boolean", "default": True},
                },
                "required": ["relative_path", "search", "replace"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_grep_file",
            "description": "Search for text in a Home Assistant config file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "search": {"type": "string"},
                },
                "required": ["relative_path", "search"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_check_config",
            "description": "Run Home Assistant configuration check through Supervisor, HA API or shell fallback.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_reload_automations",
            "description": "Reload Home Assistant automations.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_reload_scripts",
            "description": "Reload Home Assistant scripts.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_reload_core_config",
            "description": "Reload Home Assistant core configuration.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_get_states",
            "description": "Get all Home Assistant entity states.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_get_state",
            "description": "Get one Home Assistant entity state.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                },
                "required": ["entity_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_list_services",
            "description": "List Home Assistant services.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_call_service",
            "description": "Call a Home Assistant service.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "service": {"type": "string"},
                    "target": {
                        "type": "object",
                        "additionalProperties": True,
                        "default": {},
                    },
                    "data": {
                        "type": "object",
                        "additionalProperties": True,
                        "default": {},
                    },
                },
                "required": ["domain", "service"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_render_template",
            "description": "Render a Home Assistant Jinja template.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "template": {"type": "string"},
                },
                "required": ["template"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ha_agent_delete_restore_state_entity",
            "description": "Delete a restored entity from Home Assistant restore state.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "backup": {"type": "boolean", "default": True},
                },
                "required": ["entity_id"],
                "additionalProperties": False,
            },
        },
    ]


@app.get("/mcp")
def mcp_get():
    return {
        "name": "ChatGPT Admin Agent",
        "version": AGENT_VERSION,
        "description": "Home Assistant admin MCP endpoint",
        "tools": mcp_tools(),
    }


@app.post("/mcp")
async def mcp_post(
    request: Request,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    body = await request.json()
    method = body.get("method")
    params = body.get("params") or {}
    request_id = body.get("id")

    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "ChatGPT Admin Agent",
                        "version": AGENT_VERSION,
                    },
                },
            }

        if method == "tools/list":
            tools = mcp_get()["tools"]
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": tool["name"],
                            "description": tool["description"],
                            "inputSchema": tool["input_schema"],
                        }
                        for tool in tools
                    ]
                },
            }

        if method == "tools/call":
            require_auth(x_admin_token, token)
            name = params.get("name")
            arguments = params.get("arguments") or {}
            auth_value = x_admin_token or token

            if name == "ha_agent_health":
                result = health()

            elif name == "ha_agent_read_file":
                result = fs_read(
                    FilePathRequest(**arguments),
                    x_admin_token=auth_value,
                )

            elif name == "ha_agent_write_file":
                result = fs_write(
                    WriteFileRequest(**arguments),
                    x_admin_token=auth_value,
                )

            elif name == "ha_agent_replace_in_file":
                result = fs_replace(
                    ReplaceInFileRequest(**arguments),
                    x_admin_token=auth_value,
                )

            elif name == "ha_agent_grep_file":
                result = fs_grep(
                    GrepRequest(**arguments),
                    x_admin_token=auth_value,
                )

            elif name == "ha_agent_check_config":
                result = ha_check_config(x_admin_token=auth_value)

            elif name == "ha_agent_reload_automations":
                result = ha_reload_automations(x_admin_token=auth_value)

            elif name == "ha_agent_reload_scripts":
                result = ha_reload_scripts(x_admin_token=auth_value)

            elif name == "ha_agent_reload_core_config":
                result = ha_reload_core_config(x_admin_token=auth_value)

            elif name == "ha_agent_get_states":
                result = ha_get_states(x_admin_token=auth_value)

            elif name == "ha_agent_get_state":
                result = ha_get_state(
                    EntityStateRequest(**arguments),
                    x_admin_token=auth_value,
                )

            elif name == "ha_agent_list_services":
                result = ha_list_services(x_admin_token=auth_value)

            elif name == "ha_agent_call_service":
                result = ha_call_service(
                    ServiceRequest(**arguments),
                    x_admin_token=auth_value,
                )

            elif name == "ha_agent_render_template":
                result = ha_render_template(
                    TemplateRequest(**arguments),
                    x_admin_token=auth_value,
                )

            elif name == "ha_agent_delete_restore_state_entity":
                result = restore_state_delete_entity(
                    DeleteRestoreStateRequest(**arguments),
                    x_admin_token=auth_value,
                )

            else:
                raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")

            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False),
                        }
                    ]
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"Unknown method: {method}",
            },
        }

    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32000,
                "message": str(exc),
            },
        }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8787)
