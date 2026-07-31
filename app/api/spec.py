"""规范 CRUD REST API（FR15）"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from app.auth.rbac import require_role
from app.mcp.verifier import spec_store
from app.schemas import Spec, SpecListResponse

logger = logging.getLogger("ai-debug-mcp.spec_api")

router = APIRouter(prefix="/api", tags=["spec"])


@router.post("/spec", dependencies=[Depends(require_role("admin", "developer"))])
def create_spec(spec: Spec):
    """创建一条期望规范"""
    try:
        spec_id = spec_store.create(spec.model_dump(exclude_none=True))
        saved = spec_store.get(spec_id)
        return {"spec_id": spec_id, "spec": saved}
    except Exception as e:
        logger.exception("创建规范失败: %s", e)
        raise HTTPException(status_code=500, detail="创建规范失败")


@router.get("/spec", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def list_specs(kind: str | None = None, target: str | None = None):
    """列出规范，可选按 kind / target 过滤"""
    try:
        specs = spec_store.list_specs(kind=kind, target=target)
        return SpecListResponse(count=len(specs), specs=specs).model_dump()
    except Exception as e:
        logger.exception("列出规范失败: %s", e)
        raise HTTPException(status_code=500, detail="列出规范失败")


@router.get("/spec/{spec_id}", dependencies=[Depends(require_role("admin", "developer", "viewer"))])
def get_spec(spec_id: str):
    """取一条规范"""
    spec = spec_store.get(spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"规范 {spec_id} 不存在")
    return spec


@router.patch("/spec/{spec_id}", dependencies=[Depends(require_role("admin", "developer"))])
def update_spec(spec_id: str, patch: dict):
    """部分更新规范（id 不可修改）"""
    patch.pop("id", None)
    updated = spec_store.update(spec_id, patch)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"规范 {spec_id} 不存在")
    return updated


@router.delete("/spec/{spec_id}", dependencies=[Depends(require_role("admin", "developer"))])
def delete_spec(spec_id: str):
    """删除一条规范"""
    deleted = spec_store.delete(spec_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"规范 {spec_id} 不存在")
    return {"deleted": spec_id}
