from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import inspect

from brainBox import CbrainBox
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# --- 模拟平台初始化
mock_node_cfg = {
    "heartbeat_config": {
        "check_interval_s": 10,
        "device_timeout_s": 60,
    },
    "storage_config": {
        "tasks_dir": "./data/tasks",
        "results_dir": "./data/results",
        "logs_dir": "./data/logs",
    },
}


def mock_progress_callback(progress="", message="", status=""):
    pass


cbrainbox_instance = CbrainBox(
    node_cfg=mock_node_cfg,
    process_comm=None,
    proc_modules_obj=None,
    progress_callback=mock_progress_callback,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """启动/停止类脑盒子异步服务（心跳、上报等）."""
    await cbrainbox_instance._manager.start()  # noqa: SLF001
    yield
    await cbrainbox_instance._manager.stop()  # noqa: SLF001


app = FastAPI(title="BrainBox Mock Platform", lifespan=lifespan)


@app.post("/api/brainBox/CbrainBox/{subfunc}")
async def handle_request(subfunc: str, request: Request):
    """
    统一转发逻辑：根据 URL 中的 subfunc 调用 CbrainBox 类中对应的方法
    """

    # 1. 检查方法是否存在
    if not hasattr(cbrainbox_instance, subfunc):
        raise HTTPException(status_code=404, detail=f"Subfunc '{subfunc}' not found in CbrainBox")

    method = getattr(cbrainbox_instance, subfunc)

    # 2. 检查是否为可调用的公开方法（排除私有方法和属性）
    if not callable(method) or subfunc.startswith("_"):
        raise HTTPException(status_code=403, detail="Access to private methods is forbidden")

    # 3. 解析请求体中的 params
    try:
        params = await request.json()
    except Exception:
        params = {}

    # 4. 执行逻辑并返回结果
    try:
        result = method(params)
        if inspect.isawaitable(result):
            result = await result
        return result
    except KeyError as e:
        return JSONResponse(
            status_code=400,
            content={"code": -1, "msg": f"Missing required parameter: {str(e)}", "data": {}},
        )
    except Exception as e:
        print(e)
        return JSONResponse(
            status_code=500,
            content={"code": -1, "msg": f"Internal Error: {str(e)}", "data": {}},
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=15001)
