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

# ---------- routes ----------
@app.post("/create-sandbox")
async def create_sandbox():
    return {"sandbox_id": Sandbox().sandbox_id}

@app.post("/execute-code")
async def execute_code(req: CodeExecutionRequest):
    urls, seen = [], set()                              # seen = {sha256}

    sb = Sandbox.connect(req.sandbox_id)
    sb.set_timeout(6000)
    result = await asyncio.to_thread(sb.run_code, req.code)

    # ---------- scan /code and upload every artifact ----------
    for f in await asyncio.to_thread(sb.files.list, "/code"):
        raw = await asyncio.to_thread(sb.files.read, f.path, format="bytes")
        sig = sha(raw)
        if sig in seen:                                 # skip exact duplicates
            continue

        # sanity-check Excel files (ZIP magic)
        if f.name.endswith((".xls", ".xlsx")) and not raw.startswith(b"PK\x03\x04"):
            raise RuntimeError(f"{f.name} corrupted (not ZIP)")

        urls.append(await upload_s3(raw, f.name))
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
