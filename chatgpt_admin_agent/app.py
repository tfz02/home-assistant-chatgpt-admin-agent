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
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


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

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_URL = "http://supervisor/core/api"
OPTIONS_PATH = Path("/data/options.json")


def load_options() -> dict:
    if OPTIONS_PATH.exists():
        return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    return {}


OPTIONS = load_options()
ADMIN_TOKEN = OPTIONS.get("admin_token", "change-me")
ALLOW_SHELL = bool(OPTIONS.get("allow_shell", True))
ALLOW_STORAGE = bool(OPTIONS.get("allow_storage", True))

app = FastAPI(title="ChatGPT Admin Agent", version="0.1.0")


def require_auth(x_admin_token: Optional[str]) -> None:
    if not ADMIN_TOKEN or ADMIN_TOKEN == "change-me":
        raise HTTPException(status_code=403, detail="Admin token not configured")
    if x_admin_token != ADMIN_TOKEN:
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
    target: dict[str, Any] = {}
    data: dict[str, Any] = {}


class DeleteRestoreStateRequest(BaseModel):
    entity_id: str
    backup: bool = True


@app.get("/health")
def health():
    return {
        "ok": True,
        "name": "ChatGPT Admin Agent",
        "version": "0.1.0",
    }


@app.post("/fs/read")
def fs_read(req: FilePathRequest, x_admin_token: Optional[str] = Header(None)):
    require_auth(x_admin_token)

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


@app.post("/fs/write")
def fs_write(req: WriteFileRequest, x_admin_token: Optional[str] = Header(None)):
    require_auth(x_admin_token)

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


@app.post("/fs/replace")
def fs_replace(req: ReplaceInFileRequest, x_admin_token: Optional[str] = Header(None)):
    require_auth(x_admin_token)

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


@app.post("/fs/grep")
def fs_grep(req: GrepRequest, x_admin_token: Optional[str] = Header(None)):
    require_auth(x_admin_token)

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


@app.post("/yaml/validate")
def yaml_validate(req: FilePathRequest, x_admin_token: Optional[str] = Header(None)):
    require_auth(x_admin_token)

    path = resolve_path(req.relative_path)

    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"ok": True}


@app.post("/json/validate")
def json_validate(req: FilePathRequest, x_admin_token: Optional[str] = Header(None)):
    require_auth(x_admin_token)

    path = resolve_path(req.relative_path)

    try:
        json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"ok": True}


@app.post("/restore_state/delete_entity")
def restore_state_delete_entity(
    req: DeleteRestoreStateRequest,
    x_admin_token: Optional[str] = Header(None),
):
    require_auth(x_admin_token)

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


@app.post("/ha/call_service")
def ha_call_service(req: ServiceRequest, x_admin_token: Optional[str] = Header(None)):
    require_auth(x_admin_token)

    if not SUPERVISOR_TOKEN:
        raise HTTPException(status_code=500, detail="SUPERVISOR_TOKEN missing")

    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {}
    payload.update(req.data or {})

    if req.target:
        payload["target"] = req.target

    url = f"{HA_URL}/services/{req.domain}/{req.service}"
    response = requests.post(url, headers=headers, json=payload, timeout=30)

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    if response.text:
        try:
            return response.json()
        except Exception:
            return {"ok": True, "text": response.text}

    return {"ok": True}


@app.post("/ha/reload_automations")
def ha_reload_automations(x_admin_token: Optional[str] = Header(None)):
    require_auth(x_admin_token)

    if not SUPERVISOR_TOKEN:
        raise HTTPException(status_code=500, detail="SUPERVISOR_TOKEN missing")

    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        f"{HA_URL}/services/automation/reload",
        headers=headers,
        json={},
        timeout=30,
    )

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    return {"ok": True}


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


@app.post("/shell/exec")
def shell_exec(req: ShellRequest, x_admin_token: Optional[str] = Header(None)):
    require_auth(x_admin_token)

    if not ALLOW_SHELL:
        raise HTTPException(status_code=403, detail="Shell access disabled")

    return run_shell(req.command, req.cwd, req.timeout)


@app.post("/ha/check_config")
def ha_check_config(x_admin_token: Optional[str] = Header(None)):
    require_auth(x_admin_token)

    if not ALLOW_SHELL:
        raise HTTPException(status_code=403, detail="Shell access disabled")

    return run_shell("ha core check", "/config", 120)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8787)
