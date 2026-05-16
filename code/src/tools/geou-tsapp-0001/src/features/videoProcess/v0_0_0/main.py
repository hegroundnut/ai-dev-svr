"""
main.py — 视频处理服务独立运行入口

提供与 manageServer / brainBox 相同风格的 FastAPI 统一路由，
便于独立调试或在不依赖平台框架时直接部署。

路由格式：POST /api/videoProcess/CvideoProcess/{subfunc}
"""

import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from videoProcess import CvideoProcess

app = FastAPI(title="VideoProcess Service")

# --- 模拟平台初始化
_mock_node_cfg = {
    "tool_package_snumber": "geou-tsapp-0001",
}


def _mock_progress_callback(progress=0, message="", status=""):
    pass


_instance = CvideoProcess(
    node_cfg=_mock_node_cfg,
    process_comm=None,
    proc_modules_obj=None,
    progress_callback=_mock_progress_callback,
)


@app.post("/api/videoProcess/CvideoProcess/{subfunc}")
async def handle_request(subfunc: str, request: Request):
    """统一转发逻辑：根据 URL 中的 subfunc 调用 CvideoProcess 对应方法。"""
    if not hasattr(_instance, subfunc):
        raise HTTPException(status_code=404, detail=f"Subfunc '{subfunc}' not found")
    method = getattr(_instance, subfunc)
    if not callable(method) or subfunc.startswith("_"):
        raise HTTPException(status_code=403, detail="Access to private methods is forbidden")
    try:
        params = await request.json()
    except Exception:
        params = {}
    try:
        result = method(params)
        return result
    except KeyError as exc:
        return JSONResponse(
            status_code=400,
            content={"code": -1, "msg": f"Missing required parameter: {exc}", "data": {}},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"code": -1, "msg": f"Internal Error: {exc}", "data": {}},
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=13212)
