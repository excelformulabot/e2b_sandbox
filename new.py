@app.post("/execute-code")
async def execute_code(data: CodeExecutionRequest):
    try:
        sandbox = Sandbox.connect(data.sandbox_id)
        sandbox.set_timeout(6000)

        result = sandbox.run_code(data.code)

        import base64

        markdown_files = []
        stdout = "\n".join(result.logs.stdout) if result.logs.stdout else ""
        stderr = "\n".join(result.logs.stderr) if result.logs.stderr else ""

        # Track uploaded filenames
        uploaded_file_names = set()

        # 1️⃣ Upload PNGs from result.results
        for idx, res in enumerate(result.results):
            if hasattr(res, "png") and res.png:
                try:
                    png_bytes = base64.b64decode(res.png)
                    file_name = f"plot{idx+1}.png"
                    s3_url = upload_to_s3_direct(png_bytes, file_name, bucket_name)
                    if s3_url:
                        uploaded_file_names.add(file_name)
                        markdown_files.append(f"![]({s3_url})")
                except Exception as e:
                    print(f"⚠️ Failed to upload plot{idx+1}.png: {e}")

        # 2️⃣ Upload ALL other files from /code and / (root)
        for dir_path in ["/code", "/"]:
            for file in sandbox.files.list(dir_path):
                if file.name in uploaded_file_names:
                    continue  # Skip files already uploaded

                try:
                    content = sandbox.files.read(file.path)
                    if isinstance(content, str):
                        content = content.encode()

                    s3_url = upload_to_s3_direct(content, os.path.basename(file.path), bucket_name, '')
                    if s3_url:
                        uploaded_file_names.add(file.name)
                        ext = os.path.splitext(file.name)[1].lower()
                        emoji = {
                            ".csv": "📊",
                            ".txt": "📝",
                            ".html": "🌐",
                            ".json": "🔢",
                            ".xlsx": "📈",
                            ".zip": "🗜️"
                        }.get(ext, "📄")
                        markdown_files.append(f"\n{emoji} [Download {file.name}]({s3_url})")
                except Exception as e:
                    print(f"⚠️ Failed to upload {file.name}: {e}")

        final_output = stdout + "\n" + "\n".join(markdown_files)

        return {
            "sandbox_id": sandbox.sandbox_id,
            "stdout": final_output,
            "stderr": stderr,
            "error": {
                "name": result.error.name if result.error else None,
                "message": result.error.value if result.error else None,
                "traceback": result.error.traceback.splitlines() if result.error and result.error.traceback else None
            }
        }

    except Exception as e:
        return {"error": str(e)}

