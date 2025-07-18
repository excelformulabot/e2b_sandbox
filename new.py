from fastapi import FastAPI
from pydantic import BaseModel
from e2b_code_interpreter import Sandbox
import boto3, datetime, asyncio, mimetypes, hashlib, base64
from fastapi import FastAPI, HTTPException


BUCKET, REGION = "excel-formulabot-rds-storage", "us-east-2"

app = FastAPI()
s3 = boto3.client("s3", region_name=REGION)

# ---------- helpers ----------
def now_tag() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")

def sha(buf: bytes) -> str:
    return hashlib.sha256(buf).hexdigest()

async def upload_s3(buf: bytes, key: str) -> str:
    mime, _ = mimetypes.guess_type(key)
    s3.put_object(
        Bucket=BUCKET,
        Key=f"code/{key}",
        Body=buf,
        ContentType=mime or "application/octet-stream",
    )
    return f"https://{BUCKET}.s3.{REGION}.amazonaws.com/code/{key}"

# ---------- request model ----------
class CodeExecutionRequest(BaseModel):
    code: str
    sandbox_id: str | None = None
    user_id: str


class CreateSandboxRequest(BaseModel):
    template_id: str

import os
@app.post("/create-sandbox")
async def create_sandbox(req: CreateSandboxRequest):
    # Step 1: Create sandbox
    sb = Sandbox(req.template_id, timeout=300)
    sb_connected = Sandbox.connect(sb.sandbox_id)
    sandbox_id = sb.sandbox_id

    await asyncio.sleep(10)
    
    # Step 2: Connect to sandbox and set timeout
    # sb_connected = Sandbox.connect(sandbox_id)
    # # sb_connected.set_timeout(6000)

    # Step 3: Safely inject credentials from Render env into the sandbox
    access_key = os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")

    setup_code = f"""
    !pip install fsspec s3fs
    
    import os
    os.environ['AWS_ACCESS_KEY_ID'] = '{access_key}'
    os.environ['AWS_SECRET_ACCESS_KEY'] = '{secret_key}'
    
    print("Environment ready: fsspec + s3fs installed, AWS credentials set.")
    """

    # Step 4: Run setup code in the new sandbox
    # await asyncio.to_thread(sb.run_code, setup_code)

    # Step 5: Return sandbox ID
    return {"sandbox_id": sandbox_id}

from e2b_code_interpreter import Sandbox, NotFoundException  # ✅ Required

from fastapi import HTTPException
from e2b_code_interpreter import Sandbox
from e2b.exceptions import SandboxException  # ✅ correct exception
import asyncio, datetime
from typing import Optional

# ---------- main route ----------
@app.post("/execute-code")
async def execute_code(req: CodeExecutionRequest):
    urls, seen = [], set()  # seen = {sha256}

    sb = Sandbox.connect(req.sandbox_id)
    sb.set_timeout(6000)
    result = await asyncio.to_thread(sb.run_code, req.code)

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")

    # Step 2: List and upload files in /code
    try:
        for f in await asyncio.to_thread(sb.files.list, "/code"):
            raw = await asyncio.to_thread(sb.files.read, f.path, format="bytes")
            sig = sha(raw)
            if sig in seen:
                continue
            if f.name.endswith((".xls", ".xlsx")) and not raw.startswith(b"PK\x03\x04"):
                raise RuntimeError(f"{f.name} corrupted (not ZIP)")

            # user_id is optional — fallback to "user" if not present
            user_part = getattr(req, "user_id", "user")
            unique_name = f"{user_part}_{timestamp}_{f.name}"
            urls.append(await upload_s3(raw, unique_name))
            seen.add(sig)

            # optional cleanup inside sandbox
            await asyncio.to_thread(
                sb.run_code,
                f"import pathlib; pathlib.Path('{f.path}').unlink(missing_ok=True)"
            )
    except Exception as upload_err:
        print(f"⚠️ Upload error: {upload_err}")

    return {
        "sandbox_id": sb.sandbox_id,
        "stdout": "\n".join(result.logs.stdout or []),
        "stderr": "\n".join(result.logs.stderr or []),
        "file_urls": urls,
        "error": {
            "name": result.error.name if result.error else None,
            "message": result.error.value if result.error else None,
            "traceback": result.error.traceback.splitlines()
            if result.error and result.error.traceback else None,
        },
    }


from time import perf_counter  # ⏱️ To measure time precisely

class PauseRequest(BaseModel):
    sandbox_id: str

@app.post("/pause-sandbox")
async def pause_sandbox(req: PauseRequest):
    try:
        # Step 1: Connect to the existing sandbox
        sb = Sandbox.connect(req.sandbox_id)

        # Step 2: Start timing
        start = perf_counter()

        # Step 3: Pause the sandbox (blocking until done)
        paused_id = sb.pause()

        # Step 4: Stop timing
        end = perf_counter()
        duration = round(end - start, 2)  # seconds, 2 decimal places

        return {
            "message": f"Sandbox {paused_id} paused successfully.",
            "paused_id": paused_id,
            "time_taken_seconds": duration
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
