from fastapi import FastAPI
from pydantic import BaseModel
from e2b_code_interpreter import Sandbox
import boto3, datetime, asyncio, mimetypes, hashlib, base64

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

# ---------- routes ----------
import os

@app.post("/create-sandbox")
async def create_sandbox():
    # Step 1: Create sandbox
    sb = Sandbox()
    sandbox_id = sb.sandbox_id

    # Step 2: Connect to sandbox and set timeout
    sb_connected = Sandbox.connect(sandbox_id)
    sb_connected.set_timeout(6000)

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
    await asyncio.to_thread(sb_connected.run_code, setup_code)

    # Step 5: Return sandbox ID
    return {"sandbox_id": sandbox_id}

@app.post("/execute-code")
async def execute_code(req: CodeExecutionRequest):
    urls, seen = [], set()  # seen = {sha256}

    sb = Sandbox.connect(req.sandbox_id)
    sb.set_timeout(6000)
    result = await asyncio.to_thread(sb.run_code, req.code)

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")

    # ---------- scan /code and upload every artifact ----------
    for f in await asyncio.to_thread(sb.files.list, "/code"):
        raw = await asyncio.to_thread(sb.files.read, f.path, format="bytes")
        sig = sha(raw)
        if sig in seen:  # skip exact duplicates
            continue

        # sanity-check Excel files (ZIP magic)
        if f.name.endswith((".xls", ".xlsx")) and not raw.startswith(b"PK\x03\x04"):
            raise RuntimeError(f"{f.name} corrupted (not ZIP)")

        # >>> Update this line to use a unique file name
        unique_name = f"{f.name}_{req.user_id}_{timestamp}"
        urls.append(await upload_s3(raw, unique_name))
        seen.add(sig)

        # optional cleanup inside sandbox
        await asyncio.to_thread(
            sb.run_code,
            f"import pathlib; pathlib.Path('{f.path}').unlink(missing_ok=True)"
        )

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
