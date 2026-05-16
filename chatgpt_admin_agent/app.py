import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

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
AGENT_VERSION = "0.1.9"

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


def parse_json_object(value: Optional[str], field_name: str) -> dict[str, Any]:
    if value is None or str(value).strip() == "":
        return {}

    try:
        parsed = json.loads(value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} is not valid JSON: {exc}")

    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a JSON object")

    return parsed


def parse_json_any(value: Optional[str], field_name: str) -> Any:
    if value is None or str(value).strip() == "":
        return None

    try:
        return json.loads(value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} is not valid JSON: {exc}")


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


def supervisor_headers() -> dict:
    if not SUPERVISOR_TOKEN:
        raise HTTPException(status_code=500, detail="SUPERVISOR_TOKEN unavailable")

    return {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }


def ha_get(path: str):
    url = f"{ha_api_base_url()}{path}"

    try:
        response = requests.get(url, headers=ha_headers(), timeout=60)

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


def ha_post(path: str, payload: Optional[dict] = None, timeout: int = 60):
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


def supervisor_get(path: str):
    url = f"{SUPERVISOR_URL}{path}"

    try:
        response = requests.get(url, headers=supervisor_headers(), timeout=60)

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


def supervisor_post(path: str, payload: Optional[dict] = None, timeout: int = 60):
    url = f"{SUPERVISOR_URL}{path}"

    try:
        response = requests.post(
            url,
            headers=supervisor_headers(),
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


def safe_storage_file(name: str) -> Path:
    if not ALLOW_STORAGE:
        raise HTTPException(status_code=403, detail="Storage access disabled")

    if "/" in name or "\\" in name or name.strip() in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid storage file name")

    return CONFIG_ROOT / ".storage" / name.strip()


def read_json_file(path: Path) -> Any:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in {path}: {exc}")


def read_yaml_file(path: Path) -> Any:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML in {path}: {exc}")


def run_shell(command: str, cwd: str, timeout: int):
    cwd_path = resolve_path(cwd)

    dangerous_fragments = [
        "rm -rf /",
        "mkfs",
        "dd if=",
        ":(){",
        "shutdown",
        "poweroff",
        "> /dev/sd",
        "wipefs",
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
        "stdout": result.stdout[-50000:],
        "stderr": result.stderr[-50000:],
    }


class FilePathRequest(BaseModel):
    relative_path: str


class ListDirectoryRequest(BaseModel):
    relative_path: str = "."
    recursive: bool = False
    max_items: int = 500


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
    return_response: bool = False


class ServiceV2Request(BaseModel):
    domain: str
    service: str
    entity_id: str = ""
    target_json: str = ""
    data_json: str = ""
    return_response: bool = False


class DirectServiceRequest(BaseModel):
    target: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    return_response: bool = False


class TemplateRequest(BaseModel):
    template: str


class EntityStateRequest(BaseModel):
    entity_id: str


class EntityFilterRequest(BaseModel):
    domain: str = ""
    search: str = ""
    device_class: str = ""
    state: str = ""
    unavailable_only: bool = False
    limit: int = 500


class DeleteRestoreStateRequest(BaseModel):
    entity_id: str
    backup: bool = True


class StorageFileRequest(BaseModel):
    name: str


class HistoryRequest(BaseModel):
    start_time: str = ""
    end_time: str = ""
    entity_id: str = ""
    minimal_response: bool = True
    no_attributes: bool = True
    significant_changes_only: bool = False


class LogbookRequest(BaseModel):
    start_time: str = ""
    end_time: str = ""
    entity_id: str = ""


class LogRequest(BaseModel):
    relative_path: str = "home-assistant.log"
    lines: int = 300
    search: str = ""


class SelectOptionRequest(BaseModel):
    entity_id: str
    option: str


class NumberSetRequest(BaseModel):
    entity_id: str
    value: float


class ButtonPressRequest(BaseModel):
    entity_id: str


class SwitchRequest(BaseModel):
    entity_id: str
    state: str


class LightRequest(BaseModel):
    entity_id: str
    state: str = "on"
    brightness_pct: Optional[int] = None
    rgb_json: str = ""
    color_temp_kelvin: Optional[int] = None
    effect: str = ""


class ClimateTemperatureRequest(BaseModel):
    entity_id: str
    temperature: float


class VacuumCommandRequest(BaseModel):
    entity_id: str
    command: str
    fan_speed: str = ""


class AddonRequest(BaseModel):
    slug: str


class AddonCommandRequest(BaseModel):
    slug: str
    command: str


class ScriptRunRequest(BaseModel):
    entity_id: str
    data_json: str = ""


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
        "allow_storage": ALLOW_STORAGE,
        "allow_shell": ALLOW_SHELL,
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


@app.post("/fs/list", operation_id="ha_agent_list_directory")
def fs_list(
    req: ListDirectoryRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    path = resolve_path(req.relative_path)

    if not path.exists():
        raise HTTPException(status_code=404, detail="Directory not found")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")

    items = []
    iterator = path.rglob("*") if req.recursive else path.iterdir()

    for item in iterator:
        if len(items) >= max(1, min(req.max_items, 5000)):
            break

        try:
            stat = item.stat()
            item_type = "directory" if item.is_dir() else "file"
            items.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "relative_to_config": str(item.relative_to(CONFIG_ROOT)) if CONFIG_ROOT in item.parents or item == CONFIG_ROOT else None,
                    "type": item_type,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
        except Exception:
            continue

    return {
        "ok": True,
        "path": str(path),
        "count": len(items),
        "items": items,
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
        "matches": matches[:500],
    }


@app.post("/fs/log", operation_id="ha_agent_read_log")
def fs_log(
    req: LogRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    path = resolve_path(req.relative_path)

    if not path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")
    if not path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    if req.search:
        lines = [line for line in lines if req.search.lower() in line.lower()]

    limit = max(1, min(req.lines, 5000))
    selected = lines[-limit:]

    return {
        "ok": True,
        "path": str(path),
        "lines": selected,
        "count": len(selected),
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


@app.post("/storage/read", operation_id="ha_agent_read_storage_file")
def storage_read(
    req: StorageFileRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    path = safe_storage_file(req.name)
    data = read_json_file(path)

    return {
        "ok": True,
        "name": req.name,
        "path": str(path),
        "data": data,
    }


@app.post("/storage/list", operation_id="ha_agent_list_storage_files")
def storage_list(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    if not ALLOW_STORAGE:
        raise HTTPException(status_code=403, detail="Storage access disabled")

    path = CONFIG_ROOT / ".storage"

    if not path.exists():
        return {"ok": True, "count": 0, "files": []}

    files = []
    for item in sorted(path.iterdir()):
        if item.is_file():
            stat = item.stat()
            files.append(
                {
                    "name": item.name,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )

    return {
        "ok": True,
        "count": len(files),
        "files": files,
    }


@app.post("/registry/entity", operation_id="ha_agent_list_entity_registry")
def registry_entity(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return storage_read(StorageFileRequest(name="core.entity_registry"), x_admin_token=x_admin_token, token=token)


@app.post("/registry/device", operation_id="ha_agent_list_device_registry")
def registry_device(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return storage_read(StorageFileRequest(name="core.device_registry"), x_admin_token=x_admin_token, token=token)


@app.post("/registry/area", operation_id="ha_agent_list_area_registry")
def registry_area(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return storage_read(StorageFileRequest(name="core.area_registry"), x_admin_token=x_admin_token, token=token)


@app.post("/registry/label", operation_id="ha_agent_list_label_registry")
def registry_label(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return storage_read(StorageFileRequest(name="core.label_registry"), x_admin_token=x_admin_token, token=token)


@app.post("/registry/config_entries", operation_id="ha_agent_list_config_entries")
def registry_config_entries(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return storage_read(StorageFileRequest(name="core.config_entries"), x_admin_token=x_admin_token, token=token)


@app.post("/restore_state/delete_entity", operation_id="ha_agent_delete_restore_state_entity")
def restore_state_delete_entity(
    req: DeleteRestoreStateRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    path = safe_storage_file("core.restore_state")

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


@app.post("/ha/filter_entities", operation_id="ha_agent_filter_entities")
def ha_filter_entities(
    req: EntityFilterRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    states = ha_get("/states")
    results = []

    domain = req.domain.strip().lower()
    search = req.search.strip().lower()
    device_class = req.device_class.strip().lower()
    wanted_state = req.state.strip().lower()

    for item in states:
        entity_id = item.get("entity_id", "")
        state = str(item.get("state", ""))
        attrs = item.get("attributes", {}) or {}
        friendly_name = str(attrs.get("friendly_name", ""))

        if domain and not entity_id.startswith(f"{domain}."):
            continue

        if search and search not in entity_id.lower() and search not in friendly_name.lower():
            continue

        if device_class and str(attrs.get("device_class", "")).lower() != device_class:
            continue

        if wanted_state and state.lower() != wanted_state:
            continue

        if req.unavailable_only and state not in {"unavailable", "unknown"}:
            continue

        results.append(item)

        if len(results) >= max(1, min(req.limit, 5000)):
            break

    return {
        "ok": True,
        "count": len(results),
        "entities": results,
    }


@app.get("/ha/services", operation_id="ha_agent_list_services")
def ha_list_services(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)
    return ha_get("/services")


def build_service_payload(target: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    payload.update(data or {})

    if target:
        payload["target"] = target

    return payload


@app.post("/ha/call_service", operation_id="ha_agent_call_service")
def ha_call_service(
    req: ServiceRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    payload = build_service_payload(req.target, req.data)
    path = f"/services/{req.domain}/{req.service}"

    if req.return_response:
        path = f"{path}?return_response"

    return ha_post(path, payload)


@app.post("/ha/call_service_v2", operation_id="ha_agent_call_service_v2")
def ha_call_service_v2(
    req: ServiceV2Request,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    target = parse_json_object(req.target_json, "target_json")
    data = parse_json_object(req.data_json, "data_json")

    if req.entity_id.strip():
        target["entity_id"] = req.entity_id.strip()

    payload = build_service_payload(target, data)
    path = f"/services/{req.domain}/{req.service}"

    if req.return_response:
        path = f"{path}?return_response"

    return ha_post(path, payload)


@app.post("/ha/services/{domain}/{service}", operation_id="ha_agent_call_service_direct")
def ha_call_service_direct(
    domain: str,
    service: str,
    req: DirectServiceRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    payload = build_service_payload(req.target, req.data)
    path = f"/services/{domain}/{service}"

    if req.return_response:
        path = f"{path}?return_response"

    return ha_post(path, payload)


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
            timeout=60,
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


@app.post("/ha/history", operation_id="ha_agent_get_history")
def ha_history(
    req: HistoryRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    start = req.start_time.strip()
    path = "/history/period"

    if start:
        path += f"/{start}"

    params: dict[str, Any] = {}

    if req.end_time.strip():
        params["end_time"] = req.end_time.strip()

    if req.entity_id.strip():
        params["filter_entity_id"] = req.entity_id.strip()

    if req.minimal_response:
        params["minimal_response"] = "true"

    if req.no_attributes:
        params["no_attributes"] = "true"

    if req.significant_changes_only:
        params["significant_changes_only"] = "true"

    if params:
        path += "?" + urlencode(params)

    return ha_get(path)


@app.post("/ha/logbook", operation_id="ha_agent_get_logbook")
def ha_logbook(
    req: LogbookRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    start = req.start_time.strip()
    path = "/logbook"

    if start:
        path += f"/{start}"

    params: dict[str, Any] = {}

    if req.end_time.strip():
        params["end_time"] = req.end_time.strip()

    if req.entity_id.strip():
        params["entity"] = req.entity_id.strip()

    if params:
        path += "?" + urlencode(params)

    return ha_get(path)


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


@app.post("/ha/check_config", operation_id="ha_agent_check_config")
def ha_check_config(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    errors = []

    if SUPERVISOR_TOKEN:
        candidates = [
            f"{SUPERVISOR_URL}/core/check_config",
            f"{SUPERVISOR_URL}/core/check",
        ]

        for url in candidates:
            try:
                response = requests.post(url, headers=supervisor_headers(), json={}, timeout=120)

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


@app.post("/ha/list_automations", operation_id="ha_agent_list_automations")
def ha_list_automations(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    states = ha_get("/states")
    state_automations = [item for item in states if item.get("entity_id", "").startswith("automation.")]

    yaml_data = None
    yaml_path = CONFIG_ROOT / "automations.yaml"

    if yaml_path.exists():
        try:
            yaml_data = read_yaml_file(yaml_path)
        except Exception as exc:
            yaml_data = {"error": str(exc)}

    return {
        "ok": True,
        "state_count": len(state_automations),
        "states": state_automations,
        "automations_yaml": yaml_data,
    }


@app.post("/ha/list_scripts", operation_id="ha_agent_list_scripts")
def ha_list_scripts(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    states = ha_get("/states")
    state_scripts = [item for item in states if item.get("entity_id", "").startswith("script.")]

    yaml_data = None
    yaml_path = CONFIG_ROOT / "scripts.yaml"

    if yaml_path.exists():
        try:
            yaml_data = read_yaml_file(yaml_path)
        except Exception as exc:
            yaml_data = {"error": str(exc)}

    return {
        "ok": True,
        "state_count": len(state_scripts),
        "states": state_scripts,
        "scripts_yaml": yaml_data,
    }


@app.post("/ha/run_script", operation_id="ha_agent_run_script")
def ha_run_script(
    req: ScriptRunRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    data = parse_json_object(req.data_json, "data_json")
    return ha_post(
        "/services/script/turn_on",
        {
            "target": {"entity_id": req.entity_id},
            **data,
        },
    )


@app.post("/ha/select_option", operation_id="ha_agent_select_option")
def ha_select_option(
    req: SelectOptionRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    return ha_post(
        "/services/select/select_option",
        {
            "target": {"entity_id": req.entity_id},
            "option": req.option,
        },
    )


@app.post("/ha/number_set_value", operation_id="ha_agent_number_set_value")
def ha_number_set_value(
    req: NumberSetRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    return ha_post(
        "/services/number/set_value",
        {
            "target": {"entity_id": req.entity_id},
            "value": req.value,
        },
    )


@app.post("/ha/button_press", operation_id="ha_agent_button_press")
def ha_button_press(
    req: ButtonPressRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    return ha_post(
        "/services/button/press",
        {
            "target": {"entity_id": req.entity_id},
        },
    )


@app.post("/ha/switch_set", operation_id="ha_agent_switch_set")
def ha_switch_set(
    req: SwitchRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    wanted = req.state.strip().lower()
    if wanted not in {"on", "off", "toggle"}:
        raise HTTPException(status_code=400, detail="state must be on, off or toggle")

    service = "toggle" if wanted == "toggle" else f"turn_{wanted}"

    return ha_post(
        f"/services/switch/{service}",
        {
            "target": {"entity_id": req.entity_id},
        },
    )


@app.post("/ha/light_set", operation_id="ha_agent_light_set")
def ha_light_set(
    req: LightRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    wanted = req.state.strip().lower()

    if wanted not in {"on", "off", "toggle"}:
        raise HTTPException(status_code=400, detail="state must be on, off or toggle")

    service = "toggle" if wanted == "toggle" else f"turn_{wanted}"
    payload: dict[str, Any] = {"target": {"entity_id": req.entity_id}}

    if wanted == "on":
        if req.brightness_pct is not None:
            payload["brightness_pct"] = req.brightness_pct

        if req.color_temp_kelvin is not None:
            payload["color_temp_kelvin"] = req.color_temp_kelvin

        if req.effect.strip():
            payload["effect"] = req.effect.strip()

        rgb = parse_json_any(req.rgb_json, "rgb_json")
        if rgb is not None:
            if not isinstance(rgb, list) or len(rgb) != 3:
                raise HTTPException(status_code=400, detail="rgb_json must be a JSON array like [255,160,0]")
            payload["rgb_color"] = rgb

    return ha_post(f"/services/light/{service}", payload)


@app.post("/ha/climate_set_temperature", operation_id="ha_agent_climate_set_temperature")
def ha_climate_set_temperature(
    req: ClimateTemperatureRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    return ha_post(
        "/services/climate/set_temperature",
        {
            "target": {"entity_id": req.entity_id},
            "temperature": req.temperature,
        },
    )


@app.post("/ha/vacuum_command", operation_id="ha_agent_vacuum_command")
def ha_vacuum_command(
    req: VacuumCommandRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    allowed = {
        "start": "start",
        "pause": "pause",
        "stop": "stop",
        "return_to_base": "return_to_base",
        "locate": "locate",
        "clean_spot": "clean_spot",
        "set_fan_speed": "set_fan_speed",
    }

    command = req.command.strip()

    if command not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported vacuum command: {command}")

    payload: dict[str, Any] = {
        "target": {"entity_id": req.entity_id},
    }

    if command == "set_fan_speed":
        if not req.fan_speed.strip():
            raise HTTPException(status_code=400, detail="fan_speed required for set_fan_speed")
        payload["fan_speed"] = req.fan_speed.strip()

    return ha_post(f"/services/vacuum/{allowed[command]}", payload)


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


@app.post("/supervisor/info", operation_id="ha_agent_supervisor_info")
def supervisor_info(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    if SUPERVISOR_TOKEN:
        return supervisor_get("/info")

    if ALLOW_SHELL:
        return run_shell("ha supervisor info", "/config", 60)

    raise HTTPException(status_code=500, detail="Supervisor unavailable and shell disabled")


@app.post("/addons/list", operation_id="ha_agent_list_addons")
def addons_list(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    if SUPERVISOR_TOKEN:
        return supervisor_get("/addons")

    if ALLOW_SHELL:
        return run_shell("ha addons list", "/config", 60)

    raise HTTPException(status_code=500, detail="Supervisor unavailable and shell disabled")


@app.post("/addons/info", operation_id="ha_agent_addon_info")
def addon_info(
    req: AddonRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    if SUPERVISOR_TOKEN:
        return supervisor_get(f"/addons/{req.slug}/info")

    if ALLOW_SHELL:
        return run_shell(f"ha addons info {req.slug}", "/config", 60)

    raise HTTPException(status_code=500, detail="Supervisor unavailable and shell disabled")


@app.post("/addons/logs", operation_id="ha_agent_addon_logs")
def addon_logs(
    req: AddonRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    if SUPERVISOR_TOKEN:
        return supervisor_get(f"/addons/{req.slug}/logs")

    if ALLOW_SHELL:
        return run_shell(f"ha addons logs {req.slug}", "/config", 60)

    raise HTTPException(status_code=500, detail="Supervisor unavailable and shell disabled")


@app.post("/addons/command", operation_id="ha_agent_addon_command")
def addon_command(
    req: AddonCommandRequest,
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    require_auth(x_admin_token, token)

    command = req.command.strip().lower()

    allowed = {
        "start": "start",
        "stop": "stop",
        "restart": "restart",
        "rebuild": "rebuild",
        "update": "update",
    }

    if command not in allowed:
        raise HTTPException(status_code=400, detail="command must be start, stop, restart, rebuild or update")

    if SUPERVISOR_TOKEN:
        return supervisor_post(f"/addons/{req.slug}/{allowed[command]}", {})

    if ALLOW_SHELL:
        return run_shell(f"ha addons {allowed[command]} {req.slug}", "/config", 180)

    raise HTTPException(status_code=500, detail="Supervisor unavailable and shell disabled")


def tool_schema(properties: dict[str, Any], required: Optional[list[str]] = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "ha_agent_health",
            "description": "Check whether the ChatGPT Admin Agent is reachable.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_read_file",
            "description": "Read a Home Assistant file from an allowed path.",
            "input_schema": tool_schema(
                {
                    "relative_path": {"type": "string"},
                },
                ["relative_path"],
            ),
        },
        {
            "name": "ha_agent_list_directory",
            "description": "List files/directories in an allowed Home Assistant path.",
            "input_schema": tool_schema(
                {
                    "relative_path": {"type": "string", "default": "."},
                    "recursive": {"type": "boolean", "default": False},
                    "max_items": {"type": "integer", "default": 500},
                },
            ),
        },
        {
            "name": "ha_agent_write_file",
            "description": "Write a Home Assistant file with optional backup.",
            "input_schema": tool_schema(
                {
                    "relative_path": {"type": "string"},
                    "content": {"type": "string"},
                    "backup": {"type": "boolean", "default": True},
                },
                ["relative_path", "content"],
            ),
        },
        {
            "name": "ha_agent_replace_in_file",
            "description": "Replace text in a Home Assistant config file with backup.",
            "input_schema": tool_schema(
                {
                    "relative_path": {"type": "string"},
                    "search": {"type": "string"},
                    "replace": {"type": "string"},
                    "backup": {"type": "boolean", "default": True},
                },
                ["relative_path", "search", "replace"],
            ),
        },
        {
            "name": "ha_agent_grep_file",
            "description": "Search for text in a Home Assistant config file.",
            "input_schema": tool_schema(
                {
                    "relative_path": {"type": "string"},
                    "search": {"type": "string"},
                },
                ["relative_path", "search"],
            ),
        },
        {
            "name": "ha_agent_read_log",
            "description": "Read Home Assistant log lines from a file.",
            "input_schema": tool_schema(
                {
                    "relative_path": {"type": "string", "default": "home-assistant.log"},
                    "lines": {"type": "integer", "default": 300},
                    "search": {"type": "string", "default": ""},
                },
            ),
        },
        {
            "name": "ha_agent_check_config",
            "description": "Run Home Assistant configuration check through Supervisor, HA API or shell fallback.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_validate_yaml",
            "description": "Validate a YAML file.",
            "input_schema": tool_schema(
                {
                    "relative_path": {"type": "string"},
                },
                ["relative_path"],
            ),
        },
        {
            "name": "ha_agent_validate_json",
            "description": "Validate a JSON file.",
            "input_schema": tool_schema(
                {
                    "relative_path": {"type": "string"},
                },
                ["relative_path"],
            ),
        },
        {
            "name": "ha_agent_reload_automations",
            "description": "Reload Home Assistant automations.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_reload_scripts",
            "description": "Reload Home Assistant scripts.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_reload_core_config",
            "description": "Reload Home Assistant core configuration.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_get_states",
            "description": "Get all Home Assistant entity states.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_get_state",
            "description": "Get one Home Assistant entity state.",
            "input_schema": tool_schema(
                {
                    "entity_id": {"type": "string"},
                },
                ["entity_id"],
            ),
        },
        {
            "name": "ha_agent_filter_entities",
            "description": "Filter Home Assistant entities by domain, search text, device_class or state.",
            "input_schema": tool_schema(
                {
                    "domain": {"type": "string", "default": ""},
                    "search": {"type": "string", "default": ""},
                    "device_class": {"type": "string", "default": ""},
                    "state": {"type": "string", "default": ""},
                    "unavailable_only": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 500},
                },
            ),
        },
        {
            "name": "ha_agent_list_services",
            "description": "List Home Assistant services.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_call_service_v2",
            "description": "Call a Home Assistant service. Use entity_id or target_json/data_json JSON strings.",
            "input_schema": tool_schema(
                {
                    "domain": {"type": "string"},
                    "service": {"type": "string"},
                    "entity_id": {"type": "string", "default": ""},
                    "target_json": {"type": "string", "default": ""},
                    "data_json": {"type": "string", "default": ""},
                    "return_response": {"type": "boolean", "default": False},
                },
                ["domain", "service"],
            ),
        },
        {
            "name": "ha_agent_render_template",
            "description": "Render a Home Assistant Jinja template.",
            "input_schema": tool_schema(
                {
                    "template": {"type": "string"},
                },
                ["template"],
            ),
        },
        {
            "name": "ha_agent_get_history",
            "description": "Get Home Assistant history for a time range and optional entity.",
            "input_schema": tool_schema(
                {
                    "start_time": {"type": "string", "default": ""},
                    "end_time": {"type": "string", "default": ""},
                    "entity_id": {"type": "string", "default": ""},
                    "minimal_response": {"type": "boolean", "default": True},
                    "no_attributes": {"type": "boolean", "default": True},
                    "significant_changes_only": {"type": "boolean", "default": False},
                },
            ),
        },
        {
            "name": "ha_agent_get_logbook",
            "description": "Get Home Assistant logbook entries for a time range and optional entity.",
            "input_schema": tool_schema(
                {
                    "start_time": {"type": "string", "default": ""},
                    "end_time": {"type": "string", "default": ""},
                    "entity_id": {"type": "string", "default": ""},
                },
            ),
        },
        {
            "name": "ha_agent_list_automations",
            "description": "List automation states and automations.yaml content.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_list_scripts",
            "description": "List script states and scripts.yaml content.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_run_script",
            "description": "Run a script entity with optional data_json.",
            "input_schema": tool_schema(
                {
                    "entity_id": {"type": "string"},
                    "data_json": {"type": "string", "default": ""},
                },
                ["entity_id"],
            ),
        },
        {
            "name": "ha_agent_select_option",
            "description": "Set a select entity option.",
            "input_schema": tool_schema(
                {
                    "entity_id": {"type": "string"},
                    "option": {"type": "string"},
                },
                ["entity_id", "option"],
            ),
        },
        {
            "name": "ha_agent_number_set_value",
            "description": "Set a number entity value.",
            "input_schema": tool_schema(
                {
                    "entity_id": {"type": "string"},
                    "value": {"type": "number"},
                },
                ["entity_id", "value"],
            ),
        },
        {
            "name": "ha_agent_button_press",
            "description": "Press a button entity.",
            "input_schema": tool_schema(
                {
                    "entity_id": {"type": "string"},
                },
                ["entity_id"],
            ),
        },
        {
            "name": "ha_agent_switch_set",
            "description": "Turn a switch on/off or toggle it.",
            "input_schema": tool_schema(
                {
                    "entity_id": {"type": "string"},
                    "state": {"type": "string"},
                },
                ["entity_id", "state"],
            ),
        },
        {
            "name": "ha_agent_light_set",
            "description": "Turn a light on/off/toggle with optional brightness, RGB, color temperature and effect.",
            "input_schema": tool_schema(
                {
                    "entity_id": {"type": "string"},
                    "state": {"type": "string", "default": "on"},
                    "brightness_pct": {"type": "integer"},
                    "rgb_json": {"type": "string", "default": ""},
                    "color_temp_kelvin": {"type": "integer"},
                    "effect": {"type": "string", "default": ""},
                },
                ["entity_id"],
            ),
        },
        {
            "name": "ha_agent_climate_set_temperature",
            "description": "Set a climate target temperature.",
            "input_schema": tool_schema(
                {
                    "entity_id": {"type": "string"},
                    "temperature": {"type": "number"},
                },
                ["entity_id", "temperature"],
            ),
        },
        {
            "name": "ha_agent_vacuum_command",
            "description": "Control a vacuum entity.",
            "input_schema": tool_schema(
                {
                    "entity_id": {"type": "string"},
                    "command": {"type": "string"},
                    "fan_speed": {"type": "string", "default": ""},
                },
                ["entity_id", "command"],
            ),
        },
        {
            "name": "ha_agent_list_storage_files",
            "description": "List Home Assistant .storage files.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_read_storage_file",
            "description": "Read one Home Assistant .storage file by name.",
            "input_schema": tool_schema(
                {
                    "name": {"type": "string"},
                },
                ["name"],
            ),
        },
        {
            "name": "ha_agent_list_entity_registry",
            "description": "Read core.entity_registry.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_list_device_registry",
            "description": "Read core.device_registry.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_list_area_registry",
            "description": "Read core.area_registry.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_list_label_registry",
            "description": "Read core.label_registry.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_list_config_entries",
            "description": "Read core.config_entries.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_delete_restore_state_entity",
            "description": "Delete a restored entity from Home Assistant restore state.",
            "input_schema": tool_schema(
                {
                    "entity_id": {"type": "string"},
                    "backup": {"type": "boolean", "default": True},
                },
                ["entity_id"],
            ),
        },
        {
            "name": "ha_agent_supervisor_info",
            "description": "Get Supervisor info, using Supervisor token or HA CLI fallback.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_list_addons",
            "description": "List Home Assistant add-ons, using Supervisor token or HA CLI fallback.",
            "input_schema": tool_schema({}),
        },
        {
            "name": "ha_agent_addon_info",
            "description": "Get add-on info by slug.",
            "input_schema": tool_schema(
                {
                    "slug": {"type": "string"},
                },
                ["slug"],
            ),
        },
        {
            "name": "ha_agent_addon_logs",
            "description": "Get add-on logs by slug.",
            "input_schema": tool_schema(
                {
                    "slug": {"type": "string"},
                },
                ["slug"],
            ),
        },
        {
            "name": "ha_agent_addon_command",
            "description": "Run an add-on command: start, stop, restart, rebuild or update.",
            "input_schema": tool_schema(
                {
                    "slug": {"type": "string"},
                    "command": {"type": "string"},
                },
                ["slug", "command"],
            ),
        },
        {
            "name": "ha_agent_shell_exec",
            "description": "Execute a shell command if shell access is enabled.",
            "input_schema": tool_schema(
                {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "default": "/config"},
                    "timeout": {"type": "integer", "default": 30},
                },
                ["command"],
            ),
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


def call_tool_by_name(name: str, arguments: dict[str, Any], auth_value: Optional[str]):
    if name == "ha_agent_health":
        return health()

    if name == "ha_agent_read_file":
        return fs_read(FilePathRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_list_directory":
        return fs_list(ListDirectoryRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_write_file":
        return fs_write(WriteFileRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_replace_in_file":
        return fs_replace(ReplaceInFileRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_grep_file":
        return fs_grep(GrepRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_read_log":
        return fs_log(LogRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_validate_yaml":
        return yaml_validate(FilePathRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_validate_json":
        return json_validate(FilePathRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_check_config":
        return ha_check_config(x_admin_token=auth_value)

    if name == "ha_agent_reload_automations":
        return ha_reload_automations(x_admin_token=auth_value)

    if name == "ha_agent_reload_scripts":
        return ha_reload_scripts(x_admin_token=auth_value)

    if name == "ha_agent_reload_core_config":
        return ha_reload_core_config(x_admin_token=auth_value)

    if name == "ha_agent_get_states":
        return ha_get_states(x_admin_token=auth_value)

    if name == "ha_agent_get_state":
        return ha_get_state(EntityStateRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_filter_entities":
        return ha_filter_entities(EntityFilterRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_list_services":
        return ha_list_services(x_admin_token=auth_value)

    if name == "ha_agent_call_service":
        return ha_call_service(ServiceRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_call_service_v2":
        return ha_call_service_v2(ServiceV2Request(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_render_template":
        return ha_render_template(TemplateRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_get_history":
        return ha_history(HistoryRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_get_logbook":
        return ha_logbook(LogbookRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_list_automations":
        return ha_list_automations(x_admin_token=auth_value)

    if name == "ha_agent_list_scripts":
        return ha_list_scripts(x_admin_token=auth_value)

    if name == "ha_agent_run_script":
        return ha_run_script(ScriptRunRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_select_option":
        return ha_select_option(SelectOptionRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_number_set_value":
        return ha_number_set_value(NumberSetRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_button_press":
        return ha_button_press(ButtonPressRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_switch_set":
        return ha_switch_set(SwitchRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_light_set":
        return ha_light_set(LightRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_climate_set_temperature":
        return ha_climate_set_temperature(ClimateTemperatureRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_vacuum_command":
        return ha_vacuum_command(VacuumCommandRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_list_storage_files":
        return storage_list(x_admin_token=auth_value)

    if name == "ha_agent_read_storage_file":
        return storage_read(StorageFileRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_list_entity_registry":
        return registry_entity(x_admin_token=auth_value)

    if name == "ha_agent_list_device_registry":
        return registry_device(x_admin_token=auth_value)

    if name == "ha_agent_list_area_registry":
        return registry_area(x_admin_token=auth_value)

    if name == "ha_agent_list_label_registry":
        return registry_label(x_admin_token=auth_value)

    if name == "ha_agent_list_config_entries":
        return registry_config_entries(x_admin_token=auth_value)

    if name == "ha_agent_delete_restore_state_entity":
        return restore_state_delete_entity(DeleteRestoreStateRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_supervisor_info":
        return supervisor_info(x_admin_token=auth_value)

    if name == "ha_agent_list_addons":
        return addons_list(x_admin_token=auth_value)

    if name == "ha_agent_addon_info":
        return addon_info(AddonRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_addon_logs":
        return addon_logs(AddonRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_addon_command":
        return addon_command(AddonCommandRequest(**arguments), x_admin_token=auth_value)

    if name == "ha_agent_shell_exec":
        return shell_exec(ShellRequest(**arguments), x_admin_token=auth_value)

    raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")


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

            result = call_tool_by_name(name, arguments, auth_value)

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
