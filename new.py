from fastapi import FastAPI, Request
from pydantic import BaseModel
from e2b_code_interpreter import Sandbox
import os
import requests
import boto3
import datetime
from io import BytesIO
import asyncio

bucket_name = "code-interpreter-s3"
region = "us-east-2"
app = FastAPI()
s3 = boto3.client("s3", region_name=region)

async def upload_to_s3_direct_async(content: bytes, file_name: str, bucket_name: str, s3_folder="code"):
    def _upload():
        s3_client = boto3.client(
            's3',
            region_name=region,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
        )
        s3_key = f"{s3_folder}/{file_name}" if s3_folder else file_name
        try:
            s3_client.put_object(Bucket=bucket_name, Key=s3_key, Body=content)
        except Exception as e:
            print(f"❌ Upload failed: {e}")

    return await asyncio.to_thread(_upload)

class CodeExecutionRequest(BaseModel):
    code: str
    sandbox_id: str | None = None

@app.post("/create-sandbox")
async def create_sandbox():
    sbx = Sandbox()
    return {"sandbox_id": sbx.sandbox_id}

@app.post("/execute-code")
async def execute_code(data: CodeExecutionRequest):
    uploaded_pngs = set()
    try:
        sandbox = Sandbox.connect(data.sandbox_id)
        sandbox.set_timeout(6000)

        result = await asyncio.to_thread(sandbox.run_code, data.code)

        stdout = "\n".join(result.logs.stdout) if result.logs.stdout else ""
        stderr = "\n".join(result.logs.stderr) if result.logs.stderr else ""

        # Upload PNGs (without adding links)
        for idx, res in enumerate(result.results):
            if hasattr(res, "png") and res.png:
                try:
                    png_bytes = base64.b64decode(res.png)
                    chart_title = getattr(getattr(res, "chart", None), "title", None)
                    safe_title = chart_title.replace(" ", "_").replace("/", "_") if chart_title else f"plot{idx+1}"
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    file_name = f"{safe_title}_{timestamp}.png"
                    await upload_to_s3_direct_async(png_bytes, file_name, bucket_name)
                    uploaded_pngs.add(file_name)
                except Exception as e:
                    print(f"⚠️ Failed to upload plot{idx+1}.png: {e}")

        # Upload and delete other files (no download links returned)
        uploaded_files = await asyncio.to_thread(sandbox.files.list, "/code")
        for file in uploaded_files:
            if file.name.endswith(".png") and file.name in uploaded_pngs:
                continue
            try:
                content = await asyncio.to_thread(sandbox.files.read, file.path)
                filename = os.path.basename(file.path)
                if isinstance(content, str):
                    content = content.encode()
                await upload_to_s3_direct_async(content, filename, bucket_name, '')
                delete_code = f"import os\nos.remove('{file.path}')"
                try:
                    await asyncio.to_thread(sandbox.run_code, delete_code)
                except Exception as e:
                    print(f"⚠️ Failed to delete {file.path}: {e}")
            except Exception as e:
                print(f"⚠️ Failed to upload {file.name}: {e}")

        return {
            "sandbox_id": sandbox.sandbox_id,
            "stdout": stdout,
            "stderr": stderr,
            "error": {
                "name": result.error.name if result.error else None,
                "message": result.error.value if result.error else None,
                "traceback": result.error.traceback.splitlines() if result.error and result.error.traceback else None
            }
        }

    except Exception as e:
        return {"error": str(e)}
