from fastapi import FastAPI
from pydantic import BaseModel
from e2b_code_interpreter import Sandbox
import os, boto3, datetime, base64, asyncio, mimetypes, pathlib

BUCKET = "code-interpreter-s3"
REGION  = "us-east-2"

app = FastAPI()
s3  = boto3.client("s3", region_name=REGION)


# ---------- helpers ----------
async def upload_s3(content: bytes, key: str):
    mime, _ = mimetypes.guess_type(key)
    mime = mime or "application/octet-stream"
    try:
        s3.put_object(Bucket=BUCKET, Key=f"code/{key}", Body=content, ContentType=mime)
        return f"https://{BUCKET}.s3.{REGION}.amazonaws.com/code/{key}"
    except Exception as e:
        print("❌ S3 upload error:", e)
        return None


def now_tag():
    return datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")


# ---------- request models ----------
class CodeExecutionRequest(BaseModel):
    code: str
    sandbox_id: str | None = None


# ---------- routes ----------
@app.post("/create-sandbox")
async def create_sandbox():
    return {"sandbox_id": Sandbox().sandbox_id}


@app.post("/execute-code")
async def execute_code(req: CodeExecutionRequest):
    urls, uploaded_pngs = [], set()

    sb = Sandbox.connect(req.sandbox_id)
    sb.set_timeout(6000)

    result = await asyncio.to_thread(sb.run_code, req.code)

    # 1) Grab PNGs embedded in result.results ---------------------------
    for i, r in enumerate(result.results):
        if getattr(r, "png", None):
            try:
                data = base64.b64decode(r.png)
                title = getattr(getattr(r, "chart", None), "title", f"plot{i+1}")
                safe  = title.replace(" ", "_").replace("/", "_")
                fn    = f"{safe}_{now_tag()}.png"
                if url := await upload_s3(data, fn):
                    urls.append(url)
                    uploaded_pngs.add(fn)
            except Exception as e:
                print("⚠️ plot upload:", e)

    # 2) Walk /code and ship everything else (always binary) ------------
    files = await asyncio.to_thread(sb.files.list, "/code")
    for f in files:
        if f.name.endswith(".png") and f.name in uploaded_pngs:
            continue  # already done above

        try:
            # read raw bytes (SDK param name is 'format="bytes"')
            blob = await asyncio.to_thread(sb.files.read, f.path, format="bytes")

            # XLSX sanity check (ZIP magic): ensures we really got binary
            if f.name.endswith((".xls", ".xlsx")) and not bytes(blob).startswith(b"PK\x03\x04"):
                raise RuntimeError(f"{f.name} read as text → corrupted")

            if url := await upload_s3(bytes(blob), f.name):
                urls.append(url)

            # clean up inside sandbox
            await asyncio.to_thread(
                sb.run_code,
                f"import pathlib, os; pathlib.Path('{f.path}').unlink(missing_ok=True)"
            )
        except Exception as e:
            print(f"⚠️ file upload for {f.name}:", e)

    return {
        "sandbox_id": sb.sandbox_id,
        "stdout": "\n".join(result.logs.stdout or []),
        "stderr": "\n".join(result.logs.stderr or []),
        "file_urls": urls,
        "error": {
            "name": result.error.name if result.error else None,
            "message": result.error.value if result.error else None,
            "traceback": result.error.traceback.splitlines() if result.error and result.error.traceback else None,
        },
    }
