"""Store explicitly supplied review clips locally. A clip never approves a task."""
import math
import re
import time
import uuid

import appapi

MAX_CLIP_BYTES = 45 * 1024 * 1024
MAX_CAPTURE_SECONDS = 180
MIME_EXTENSIONS = {"video/webm": ".webm", "video/mp4": ".mp4"}


def context(project, task):
    if not re.fullmatch(r"T\d+[a-z]?", task):
        raise ValueError("Choose a valid task")
    runtime = appapi.runtime_for(project)
    task_file = runtime.safe_path("tasks/" + task + ".md", extensions=False)
    if not task_file.is_file(): raise ValueError("Task does not exist")
    import hashlib
    head = appapi._git(project, "rev-parse", "HEAD").stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", head): raise ValueError("Commit this project's initial files before attaching review clips")
    return {"task": task, "projectId": hashlib.sha256(str(runtime.root).encode()).hexdigest(), "head": head,
            "taskHash": hashlib.sha256(task_file.read_bytes()).hexdigest()}


def save(project, task, stream, length, content_type, note="", duration=None, capture_head="", task_hash="", source="upload", project_id="", *, revision="", build=""):
    if not isinstance(length, int) or not 0 < length <= MAX_CLIP_BYTES:
        raise ValueError("Choose a nonempty clip of 45 MB or less")
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime not in MIME_EXTENSIONS: raise ValueError("Use a WebM or MP4 video")
    current = context(project, task)
    if project_id != current["projectId"]: raise ValueError("Project changed or recording context is missing; reopen its review page")
    if not re.fullmatch(r"[0-9a-f]{40,64}", current["head"]): raise ValueError("This project needs a git commit before clips can be attached")
    if capture_head:
        if not re.fullmatch(r"[0-9a-f]{40,64}", capture_head) or appapi._git(project, "cat-file", "-e", capture_head + "^{commit}").returncode:
            raise ValueError("Recording's project commit is not in this repository")
    if task_hash and not re.fullmatch(r"[0-9a-f]{64}", task_hash): raise ValueError("Invalid recording task context")
    if duration is not None:
        duration = float(duration)
        if not math.isfinite(duration) or not 0 <= duration <= 86400: raise ValueError("Invalid recording duration")
    if source not in ("screen-recording", "upload"): raise ValueError("Invalid clip source")
    prefix = stream.read(min(16, length))
    if (mime == "video/webm" and not prefix.startswith(b"\x1a\x45\xdf\xa3")) or (mime == "video/mp4" and prefix[4:8] != b"ftyp"):
        raise ValueError("The clip does not have the selected video container")
    runtime = appapi.runtime_for(project)
    relative = ".relay/recordings/" + task + "/" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10] + MIME_EXTENSIONS[mime]
    destination = runtime.safe_path(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with destination.open("xb") as output:
            created = True
            output.write(prefix)
            remaining = length - len(prefix)
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk: raise ValueError("Clip upload was interrupted; retry the saved clip")
                output.write(chunk)
                remaining -= len(chunk)
        metadata = {"source": source, "savedAt": time.time(), "durationSeconds": duration,
                    "projectCommitAtStart": capture_head or None, "taskHashAtStart": task_hash or None,
                    "taskChangedDuringCapture": bool(task_hash and task_hash != current["taskHash"])}
        return runtime.add_artifact(task, relative, note[:300], media=metadata, revision=revision, build=build)
    except Exception:
        if created: destination.unlink(missing_ok=True)
        raise


def byte_range(header, size):
    """One HTTP byte range, inclusive; reject multipart and unsatisfiable ranges."""
    if not header: return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or not any(match.groups()) or size == 0: raise ValueError("Invalid byte range")
    if not match[1]:
        count = int(match[2])
        if count <= 0: raise ValueError("Invalid byte range")
        return max(0, size - count), size - 1, True
    start = int(match[1])
    end = min(size - 1, int(match[2])) if match[2] else size - 1
    if start >= size or start > end: raise ValueError("Byte range is outside the clip")
    return start, end, True
