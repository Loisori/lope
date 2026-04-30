import json
import os
import fnmatch
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from tavily import TavilyClient
except Exception:  # pragma: no cover - optional dependency
    TavilyClient = None

try:
    from duckduckgo_search import DDGS
except Exception:  # pragma: no cover - optional dependency
    DDGS = None


def _load_allowed_roots() -> List[Path]:
    env = os.getenv("ALLOWED_ROOTS", "").strip()
    roots: List[Path] = []
    if env:
        for part in env.split(","):
            part = part.strip()
            if part:
                roots.append(Path(part).expanduser().resolve())
    if not roots:
        roots = [Path("~/Documents").expanduser().resolve(), Path("~/Developer").expanduser().resolve()]
    return roots


ALLOWED_ROOTS = _load_allowed_roots()


def _resolve_allowed_path(path: str) -> Path:
    raw = Path(path).expanduser()
    target = raw if raw.is_absolute() else (Path.home() / raw)
    target = target.resolve(strict=False)

    for root in ALLOWED_ROOTS:
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    raise ValueError(f"Path not allowed: {path}")


def _default_cwd() -> Path:
    return ALLOWED_ROOTS[0] if ALLOWED_ROOTS else Path.home()


def list_files(path: str, recursive: bool = False, max_entries: int = 200) -> Dict[str, Any]:
    target = _resolve_allowed_path(path)
    if not target.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if not target.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")

    entries: List[Dict[str, Any]] = []

    if recursive:
        iterator = target.rglob("*")
    else:
        iterator = target.iterdir()

    for item in iterator:
        entry = {
            "name": item.name,
            "path": str(item),
            "type": "dir" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
        }
        entries.append(entry)
        if len(entries) >= max_entries:
            break

    return {
        "root": str(target),
        "entries": entries,
        "truncated": len(entries) >= max_entries,
    }


def read_file(path: str, max_bytes: int = 200_000) -> Dict[str, Any]:
    target = _resolve_allowed_path(path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    with target.open("rb") as handle:
        content = handle.read(max_bytes)

    return {
        "path": str(target),
        "content": content.decode("utf-8", errors="replace"),
        "truncated": target.stat().st_size > max_bytes,
    }


def write_file(path: str, content: str, overwrite: bool = False, create_dirs: bool = True) -> Dict[str, Any]:
    target = _resolve_allowed_path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"File exists: {path}")
    if create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)

    with target.open("w", encoding="utf-8") as handle:
        handle.write(content)

    return {"path": str(target), "bytes_written": len(content.encode("utf-8"))}


def search_files(
    root: str,
    pattern: str,
    content_query: Optional[str] = None,
    max_results: int = 50,
    max_bytes: int = 200_000,
) -> Dict[str, Any]:
    base = _resolve_allowed_path(root)
    if not base.exists() or not base.is_dir():
        raise FileNotFoundError(f"Root not found: {root}")

    results: List[Dict[str, Any]] = []

    for path in base.rglob("*"):
        if path.is_dir():
            continue
        name_match = fnmatch.fnmatch(path.name, pattern) or pattern in path.name
        if not name_match:
            continue

        content_match = True
        if content_query:
            try:
                with path.open("rb") as handle:
                    content = handle.read(max_bytes).decode("utf-8", errors="ignore")
                content_match = content_query in content
            except Exception:
                content_match = False

        if not content_match:
            continue

        results.append({"path": str(path), "size": path.stat().st_size})
        if len(results) >= max_results:
            break

    return {"root": str(base), "results": results, "truncated": len(results) >= max_results}


def run_shell(command: str, cwd: Optional[str] = None, timeout: int = 60) -> Dict[str, Any]:
    working_dir = _resolve_allowed_path(cwd) if cwd else _default_cwd()

    completed = subprocess.run(
        ["/bin/zsh", "-lc", command],
        cwd=str(working_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    return {
        "cwd": str(working_dir),
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-40000:],
        "stderr": completed.stderr[-40000:],
    }


def git_status(repo_path: str) -> Dict[str, Any]:
    result = run_shell("git status --short", cwd=repo_path)
    return result


def git_commit(repo_path: str, message: str, add_all: bool = True) -> Dict[str, Any]:
    if add_all:
        run_shell("git add -A", cwd=repo_path)
    result = run_shell(f"git commit -m {json.dumps(message)}", cwd=repo_path)
    return result


def web_search(query: str, max_results: int = 5) -> Dict[str, Any]:
    tavily_key = os.getenv("TAVILY_API_KEY")
    if tavily_key and TavilyClient:
        client = TavilyClient(api_key=tavily_key)
        data = client.search(query=query, max_results=max_results)
        return {"provider": "tavily", "results": data.get("results", data)}

    if not DDGS:
        raise RuntimeError("DuckDuckGo search is not available. Install duckduckgo-search or set TAVILY_API_KEY.")

    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    return {"provider": "duckduckgo", "results": results}


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders under an allowed path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list."},
                    "recursive": {"type": "boolean", "description": "Recurse into subdirectories."},
                    "max_entries": {"type": "integer", "description": "Maximum entries to return."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from an allowed path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read."},
                    "max_bytes": {"type": "integer", "description": "Maximum bytes to read."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a text file to an allowed path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write."},
                    "content": {"type": "string", "description": "File contents."},
                    "overwrite": {"type": "boolean", "description": "Overwrite existing file."},
                    "create_dirs": {"type": "boolean", "description": "Create missing directories."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for files by name and optional content query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "root": {"type": "string", "description": "Root directory to search."},
                    "pattern": {"type": "string", "description": "Filename pattern or substring."},
                    "content_query": {"type": "string", "description": "Optional content substring."},
                    "max_results": {"type": "integer", "description": "Maximum results."},
                    "max_bytes": {"type": "integer", "description": "Maximum bytes to scan per file."},
                },
                "required": ["root", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a zsh command in an allowed working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."},
                    "cwd": {"type": "string", "description": "Working directory."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Get git status --short for a repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Path to git repo."},
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Commit changes in a git repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Path to git repo."},
                    "message": {"type": "string", "description": "Commit message."},
                    "add_all": {"type": "boolean", "description": "Run git add -A before commit."},
                },
                "required": ["repo_path", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for recent information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {"type": "integer", "description": "Maximum results."},
                },
                "required": ["query"],
            },
        },
    },
]


TOOL_REGISTRY = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "search_files": search_files,
    "run_shell": run_shell,
    "git_status": git_status,
    "git_commit": git_commit,
    "web_search": web_search,
}


def run_tool(name: str, arguments: Dict[str, Any]) -> str:
    if name not in TOOL_REGISTRY:
        raise KeyError(f"Unknown tool: {name}")
    result = TOOL_REGISTRY[name](**arguments)
    return json.dumps(result, ensure_ascii=True)
