from fastapi import FastAPI, Request
from pydantic import BaseModel
from e2b_code_interpreter import Sandbox
import os

app = FastAPI()

class CodeExecutionRequest(BaseModel):
    code: str
    sandbox_id: str | None = None

@app.post("/create-sandbox")
async def create_sandbox():
    sbx = Sandbox()
    return {"sandbox_id": sbx.sandbox_id}

@app.post("/execute-code")
async def execute_code(data: CodeExecutionRequest):
    try:
        if data.sandbox_id:
            sbx = Sandbox.connect(data.sandbox_id)
        else:
            sbx = Sandbox()
        
        sbx.set_timeout(600)  # Set max timeout

        result = sbx.run_code(data.code)

        return {
            "sandbox_id": sbx.sandbox_id,
            "stdout": result.logs.stdout,
            "stderr": result.logs.stderr,
            "error": {
                "name": result.error.name if result.error else None,
                "message": result.error.value if result.error else None,
                "traceback": result.error.traceback.splitlines() if result.error and result.error.traceback else None
            }
        }

    except Exception as e:
        return {"error": str(e)}
