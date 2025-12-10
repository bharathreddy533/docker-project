# app.py
import os
import tempfile
import subprocess
import uuid
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

MAX_CHARS = 5000
DOCKER_IMAGE = "python:3.11-slim"
TIMEOUT_SECONDS = 10  # kill long-running runs
MEMORY_LIMIT = "128m"
READ_ONLY = True

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/run", methods=["POST"])
def run_code():
    data = request.get_json() or {}
    code = data.get("code", "")

    if not isinstance(code, str):
        return jsonify({"error": "Field 'code' must be a string."}), 400

    if len(code) == 0:
        return jsonify({"error": "No code provided."}), 400

    if len(code) > MAX_CHARS:
        return jsonify({"error": f"Code too long. Max {MAX_CHARS} characters allowed."}), 400

    # Create temp dir and file
    run_id = str(uuid.uuid4())[:8]
    workdir = tempfile.mkdtemp(prefix=f"exec_{run_id}_")
    script_path = os.path.join(workdir, "script.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(code)

    # Build docker run command:
    # - --rm : remove container after exit
    # - --network none : block network
    # - --memory : limit memory
    # - --pids-limit : avoid fork bomb (optional)
    # - --read-only : make filesystem read-only (still need a tmp mount)
    # - mount the script read-only into /app
    container_tmp = "/tmp_exec"  # inside container writable tmp
    docker_cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "--memory", MEMORY_LIMIT,
        "--pids-limit", "64",
        "--cpus", "0.5",
    ]

    if READ_ONLY:
        docker_cmd += ["--read-only"]

    # Mount temp dir: script will be mounted read-only; also provide a small writable tmp dir
    # (we mount it as a tmpfs to avoid persistence; but tmpfs requires docker privileges; as fallback use a bind)
    docker_cmd += [
        "-v", f"{script_path}:/app/script.py:ro",
        "-v", f"{workdir}:/app/writable",  # container path, but container fs might be read-only; keep for debugging
    ]

    # Run python with -u (unbuffered) to capture prints in real time
    docker_cmd += [DOCKER_IMAGE, "timeout", str(TIMEOUT_SECONDS), "python", "-u", "/app/script.py"]
    # Note: using host 'timeout' binary inside container — present in many images; if missing fallback to python wrapper below

    try:
        proc = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS + 2  # slightly larger to ensure we can handle killing
        )
    except subprocess.TimeoutExpired:
        # subprocess didn't finish (e.g., docker didn't exit). Try to kill any container (best-effort).
        return jsonify({"error": f"Execution timed out after {TIMEOUT_SECONDS} seconds."}), 200

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    return_code = proc.returncode

    # Normalize common docker timeout exit codes / messages:
    if return_code == 124 or "Command terminated" in stderr:
        return jsonify({"error": f"Execution timed out after {TIMEOUT_SECONDS} seconds."}), 200

    # Security: don't leak long outputs — cap them (but still return helpful snippet)
    MAX_OUTPUT = 10000
    if len(stdout) > MAX_OUTPUT:
        stdout = stdout[:MAX_OUTPUT] + "\n... (truncated)\n"
    if len(stderr) > MAX_OUTPUT:
        stderr = stderr[:MAX_OUTPUT] + "\n... (truncated)\n"

    # Compose response
    resp = {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": return_code,
    }

    # Clean up temp files (best-effort)
    try:
        os.remove(script_path)
        os.rmdir(workdir)
    except Exception:
        pass

    return jsonify(resp), 200

if __name__ == "__main__":
    app.run(debug=True, port=5000)
