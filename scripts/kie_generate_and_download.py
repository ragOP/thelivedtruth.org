#!/usr/bin/env python3
"""Generate images with Kie Nano Banana Pro and download outputs automatically."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


CREATE_TASK_URL = "https://api.kie.ai/api/v1/jobs/createTask"
TASK_DETAIL_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate image(s) with Kie Nano Banana Pro and auto-download outputs."
    )
    parser.add_argument("--prompt", help="Single prompt to generate.")
    parser.add_argument(
        "--html-file",
        help="HTML file containing img-placeholder blocks with NANO BANANA PRO prompts.",
    )
    parser.add_argument(
        "--apply-html",
        action="store_true",
        help="Replace matched placeholder blocks in --html-file with generated <img> tags.",
    )
    parser.add_argument(
        "--reference-image",
        default="refrence.webp",
        help="Reference image path used when prompt asks to attach a product image.",
    )
    parser.add_argument(
        "--prompts-file",
        help="Text file with one prompt per line. Lines starting with # are ignored.",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/kie",
        help="Base output directory (default: outputs/kie).",
    )
    parser.add_argument(
        "--campaign",
        default="default",
        help="Subfolder label for this run (default: default).",
    )
    parser.add_argument(
        "--aspect-ratio",
        default="16:9",
        help="Aspect ratio, e.g. 16:9, 9:16, 1:1, auto.",
    )
    parser.add_argument(
        "--resolution",
        default="2K",
        choices=["1K", "2K", "4K"],
        help="Output resolution (default: 2K).",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        default="png",
        choices=["png", "jpg"],
        help="Downloaded file extension preference (default: png).",
    )
    parser.add_argument(
        "--model",
        default="nano-banana-pro",
        help="Model identifier for Kie API (default: nano-banana-pro).",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=3,
        help="Seconds between status checks (default: 3).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Max seconds to wait per task (default: 300).",
    )
    return parser.parse_args()


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def request_json(url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to reach {url}: {exc}") from exc


def request_json_get(url: str, api_key: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    req = urllib.request.Request(
        url=full_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code} from {full_url}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to reach {full_url}: {exc}") from exc


def extract_task_id(create_response: dict[str, Any]) -> str:
    candidates = [
        create_response.get("taskId"),
        create_response.get("id"),
        (create_response.get("data") or {}).get("taskId"),
        (create_response.get("data") or {}).get("id"),
    ]
    task_id = next((c for c in candidates if isinstance(c, str) and c.strip()), None)
    if not task_id:
        pretty = json.dumps(create_response, indent=2)
        raise RuntimeError(f"Could not find task id in response:\n{pretty}")
    return task_id


def looks_terminal_status(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return any(
        token in normalized
        for token in ("success", "succeed", "completed", "failed", "error", "cancel")
    )


def extract_status(detail_response: dict[str, Any]) -> str:
    for key in ("status", "taskStatus", "state"):
        value = detail_response.get(key)
        if looks_terminal_status(value):
            return str(value).lower()
    data = detail_response.get("data")
    if isinstance(data, dict):
        for key in ("status", "taskStatus", "state"):
            value = data.get(key)
            if looks_terminal_status(value):
                return str(value).lower()
    return "unknown"


def extract_urls_from_any(node: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(node, str):
        if node.startswith(("http://", "https://")):
            return [node]
        return []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                lowered = key.lower()
                if any(token in lowered for token in ("url", "image", "output", "result")):
                    urls.append(value)
            elif isinstance(value, str):
                candidate = value.strip()
                if candidate.startswith("{") or candidate.startswith("["):
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        parsed = None
                    if parsed is not None:
                        urls.extend(extract_urls_from_any(parsed))
                else:
                    urls.extend(extract_urls_from_any(value))
            else:
                urls.extend(extract_urls_from_any(value))
    elif isinstance(node, list):
        for item in node:
            urls.extend(extract_urls_from_any(item))
    return urls


def download_file(url: str, output_path: Path) -> None:
    req = urllib.request.Request(url=url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as response:
        output_path.write_bytes(response.read())


def slugify(text: str, max_len: int = 48) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    if not cleaned:
        cleaned = "image"
    return cleaned[:max_len].strip("-") or "image"


def load_prompts(single_prompt: str | None, prompts_file: str | None) -> list[str]:
    prompts: list[str] = []
    if single_prompt:
        prompts.append(single_prompt.strip())
    if prompts_file:
        for raw_line in Path(prompts_file).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                prompts.append(line)
    prompts = [p for p in prompts if p]
    if not prompts:
        raise ValueError("Provide --prompt or --prompts-file with at least one prompt.")
    return prompts


PLACEHOLDER_PATTERN = re.compile(
    r'(<div class="img-placeholder"(?:\s+style="height:(\d+)px;")?>\s*NANO BANANA PRO — 1280×720px\s*\n)(.*?)(</div>)',
    re.DOTALL,
)


def load_prompts_from_html(html_path: str) -> list[str]:
    html = Path(html_path).read_text(encoding="utf-8")
    matches = list(PLACEHOLDER_PATTERN.finditer(html))
    prompts: list[str] = []
    for match in matches:
        prompt = match.group(3).strip()
        if prompt:
            prompts.append(prompt)
    if not prompts:
        raise ValueError(f"No NANO BANANA PRO placeholder prompts found in {html_path}")
    return prompts


def apply_images_to_html(html_path: str, image_paths: list[Path], prompts: list[str]) -> None:
    html_file = Path(html_path)
    html = html_file.read_text(encoding="utf-8")
    matches = list(PLACEHOLDER_PATTERN.finditer(html))
    if len(matches) != len(image_paths):
        raise RuntimeError(
            f"Placeholder/image mismatch: placeholders={len(matches)} images={len(image_paths)}"
        )

    html_dir = html_file.parent
    result_parts: list[str] = []
    last_idx = 0

    for idx, match in enumerate(matches, start=1):
        result_parts.append(html[last_idx : match.start()])
        rel_src = os.path.relpath(image_paths[idx - 1], start=html_dir).replace("\\", "/")
        alt_text = f"Story image {idx}"
        img_tag = (
            f'<img src="{rel_src}" alt="{alt_text}" '
            f'style="width:100%;height:auto;border-radius:4px;margin:28px 0;display:block;" '
            f'loading="lazy">'
        )
        result_parts.append(img_tag)
        last_idx = match.end()

    result_parts.append(html[last_idx:])
    html_file.write_text("".join(result_parts), encoding="utf-8")


def wait_for_result(api_key: str, task_id: str, poll_interval: int, timeout: int) -> dict[str, Any]:
    started = time.time()
    use_get_method = False
    while True:
        if use_get_method:
            detail = request_json_get(TASK_DETAIL_URL, api_key, {"taskId": task_id})
        else:
            detail = request_json(TASK_DETAIL_URL, api_key, {"taskId": task_id})
            msg = str(detail.get("msg", "")).lower() if isinstance(detail, dict) else ""
            if "post request not supported" in msg:
                use_get_method = True
                detail = request_json_get(TASK_DETAIL_URL, api_key, {"taskId": task_id})
        status = extract_status(detail)
        if any(token in status for token in ("success", "succeed", "completed")):
            return detail
        if any(token in status for token in ("fail", "error", "cancel")):
            pretty = json.dumps(detail, indent=2)
            raise RuntimeError(f"Task {task_id} failed with status '{status}':\n{pretty}")
        if (time.time() - started) > timeout:
            pretty = json.dumps(detail, indent=2)
            raise TimeoutError(f"Timeout waiting for task {task_id}. Last response:\n{pretty}")
        time.sleep(poll_interval)


def run_prompt(
    api_key: str,
    prompt: str,
    args: argparse.Namespace,
    output_root: Path,
    prompt_index: int,
) -> list[Path]:
    marker_prompt = prompt.lower()
    wants_reference = "[attach snorestop product image before generating]" in marker_prompt
    payload = {
        "model": args.model,
        "input": {
            "prompt": prompt,
            "aspect_ratio": args.aspect_ratio,
            "resolution": args.resolution,
            "output_format": args.output_format,
        },
    }
    if wants_reference and args.reference_image:
        ref_value = args.reference_image.strip()
        if ref_value.startswith(("http://", "https://")):
            payload["input"]["image_input"] = [ref_value]
        elif Path(ref_value).exists():
            print(
                f"[{prompt_index}] warning: local reference image '{ref_value}' cannot be sent directly; "
                "Kie expects a public URL for image_input. Proceeding without image_input.",
                file=sys.stderr,
            )
        else:
            print(
                f"[{prompt_index}] warning: reference image not found: {args.reference_image}",
                file=sys.stderr,
            )
    create_response = request_json(CREATE_TASK_URL, api_key, payload)
    task_id = extract_task_id(create_response)
    print(f"[{prompt_index}] task created: {task_id}")

    detail_response = wait_for_result(
        api_key=api_key,
        task_id=task_id,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    )
    urls = list(dict.fromkeys(extract_urls_from_any(detail_response)))
    if not urls:
        pretty = json.dumps(detail_response, indent=2)
        raise RuntimeError(f"No output URL found for task {task_id}:\n{pretty}")

    downloaded: list[Path] = []
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{ts}_{prompt_index:02d}_{slugify(prompt)}"
    for idx, url in enumerate(urls, start=1):
        parsed = urllib.parse.urlparse(url)
        suffix = Path(parsed.path).suffix.lower() or f".{args.output_format}"
        filename = f"{base}_{idx}{suffix}"
        target = output_root / filename
        download_file(url, target)
        downloaded.append(target)
        print(f"[{prompt_index}] saved: {target}")
    return downloaded


def main() -> int:
    args = parse_args()
    load_dotenv(Path(".env"))

    api_key = os.getenv("KIE_API_KEY")
    if not api_key:
        print("Missing KIE_API_KEY. Add it to environment or .env file.", file=sys.stderr)
        return 1

    try:
        if args.html_file:
            prompts = load_prompts_from_html(args.html_file)
        else:
            prompts = load_prompts(args.prompt, args.prompts_file)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    day = datetime.now().strftime("%Y-%m-%d")
    output_root = Path(args.output_dir) / day / slugify(args.campaign, max_len=32)
    output_root.mkdir(parents=True, exist_ok=True)

    failures = 0
    downloaded_images: list[Path] = []
    for idx, prompt in enumerate(prompts, start=1):
        print(f"[{idx}] generating: {prompt[:80]}{'...' if len(prompt) > 80 else ''}")
        try:
            downloaded = run_prompt(
                api_key=api_key,
                prompt=prompt,
                args=args,
                output_root=output_root,
                prompt_index=idx,
            )
            downloaded_images.extend(downloaded)
        except Exception as exc:
            failures += 1
            print(f"[{idx}] error: {exc}", file=sys.stderr)

    if args.apply_html and args.html_file and failures == 0:
        try:
            apply_images_to_html(args.html_file, downloaded_images, prompts)
            print(f"Updated HTML with image links: {args.html_file}")
        except Exception as exc:
            print(f"Failed to update HTML: {exc}", file=sys.stderr)
            return 1

    print(f"Done. prompts={len(prompts)} failures={failures} output_dir={output_root}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
