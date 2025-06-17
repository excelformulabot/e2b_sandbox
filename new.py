from fastapi import FastAPI
from pydantic import BaseModel
from e2b_code_interpreter import Sandbox
import os, boto3, datetime, base64, asyncio, mimetypes, hashlib

BUCKET  = "code-interpreter-s3"
REGION  = "us-east-2"

app = FastAPI()
s3   = boto3.client("s3", region_name=REGION)


# ---------- helpers ----------
async def upload_s3(content: bytes, key: str):
    mime, _ = mimetypes.guess_type(key)
    mime = mime or "application/octet-stream"
    s3.put_object(Bucket=BUCKET, Key=f"code/{key}", Body=content, ContentType=mime)
    return f"https://{BUCKET}.s3.{REGION}.amazonaws.com/code/{key}"


def now_tag():
    return datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def md5(buf: bytes) -> str:
    return hashlib.md5(buf).hexdigest()


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
    urls, seen_hashes = [], set()

    sb = Sandbox.connect(req.sandbox_id)
    sb.set_timeout(6000)
    result = await asyncio.to_thread(sb.run_code, req.code)

    # 1) PNGs embedded in result.results --------------------------------
    for i, r in enumerate(result.results):
        if getattr(r, "png", None):
            raw = base64.b64decode(r.png)
            if md5(raw) in seen_hashes:
                continue
            title = getattr(getattr(r, "chart", None), "title", f"plot{i+1}")
            safe  = title.replace(" ", "_").replace("/", "_")
            fn    = f"{safe}_{now_tag()}.png"
            urls.append(await upload_s3(raw, fn))
            seen_hashes.add(md5(raw))

    # 2) Scan /code and upload everything else ---------------------------
    for f in await asyncio.to_thread(sb.files.list, "/code"):
        raw = await asyncio.to_thread(sb.files.read, f.path, format="bytes")
        if md5(raw) in seen_hashes:
            continue  # duplicate content

        # XLS/XLSX sanity check
        if f.name.endswith((".xls", ".xlsx")) and not raw.startswith(b"PK\x03\x04"):
            raise RuntimeError(f"{f.name} read as text → corrupted")

        urls.append(await upload_s3(raw, f.name))
        seen_hashes.add(md5(raw))

        # clean up
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
