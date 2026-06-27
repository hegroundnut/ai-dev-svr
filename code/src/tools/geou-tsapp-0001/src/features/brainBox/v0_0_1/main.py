"""
brainBox 独立运行入口 — 用于本地测试和调试。

启动后自动连接 manageServer 的 WebSocket，
同时暴露 HTTP API 供本地调用系统方法。
"""

import inspect
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from brainBox import CbrainBox

app = FastAPI(title="BrainBox Test Server")

# --- 模拟平台初始化
mock_node_cfg = {}


def mock_progress_callback(progress="", message="", status=""):
    pass


_instance = CbrainBox(
    node_cfg=mock_node_cfg,
    process_comm=None,
    proc_modules_obj=None,
    progress_callback=mock_progress_callback,
)


@app.post("/api/brainBox/CbrainBox/{subfunc}")
async def handle_request(subfunc: str, request: Request):
    """统一转发：根据 URL 中的 subfunc 调用 CbrainBox 对应方法。"""
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
        if inspect.isawaitable(result):
            result = await result
        return result
    except KeyError as e:
        return JSONResponse(
            status_code=400,
            content={"code": -1, "msg": f"Missing required parameter: {e}", "data": {}},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"code": -1, "msg": f"Internal Error: {e}", "data": {}},
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=15001)
