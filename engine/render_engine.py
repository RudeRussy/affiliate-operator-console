"""
ComfyUI render engine: turn a content pack's image prompts into real SDXL renders.

Reads content_out/<slug>/brief.json -> image_prompts[], builds a standard SDXL
text-to-image graph, queues it to ComfyUI (:8188), polls, and downloads the PNGs
into the pack's images/ folder.

Design notes (learned the hard way on this box):
  - Queue ONE image at a time. The 64GB machine swaps and ComfyUI dies silently if
    several heavy jobs run concurrently.
  - Always set PYTHONPATH="" if you ever restart ComfyUI from here (PIL ABI bleed).
  - Use urllib Request objects (add_header) rather than urlopen(headers=...).
  - Poll /history/<prompt_id>; images come back via /view.

Usage:
  python -m engine.render_engine --pack content_out/<slug>
  python -m engine.render_engine --pack content_out/<slug> --steps 28 --limit 2
  python -m engine.render_engine --prompt "a cat" --out /tmp/test.png
"""
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

COMFY = "http://127.0.0.1:8188"
DEFAULT_CKPT = "juggernautXL_ragnarokBy.safetensors"
DEFAULT_NEG = ("text, watermark, signature, logo, blurry, low quality, jpeg artifacts, "
               "deformed, extra limbs, bad anatomy, worst quality, lowres, "
               "person, people, human, face, hands")


def _get(path, timeout=15):
    with urllib.request.urlopen(COMFY + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(path, payload, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(COMFY + path, data=data)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def is_up() -> bool:
    try:
        _get("/system_stats", timeout=5)
        return True
    except Exception:
        return False


def list_checkpoints():
    try:
        d = _get("/object_info/CheckpointLoaderSimple")
        return d["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    except Exception:
        return []


def build_graph(prompt, negative, ckpt, width, height, steps, cfg, seed, batch=1):
    """Standard SDXL txt2img graph (no LoRAs) — safe on 64GB."""
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": batch}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["4", 1]}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": "dpmpp_2m", "scheduler": "karras",
                         "denoise": 1.0, "model": ["4", 0],
                         "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "affiliate", "images": ["8", 0]}},
    }


def queue(prompt, negative, ckpt=DEFAULT_CKPT, width=1024, height=1024,
          steps=28, cfg=5.0, seed=None, batch=1):
    if seed is None:
        seed = int(time.time() * 1000) % (2**31)
    graph = build_graph(prompt, negative, ckpt, width, height, steps, cfg, seed, batch)
    out = _post("/prompt", {"prompt": graph, "client_id": "affiliate-stack"})
    return out.get("prompt_id"), seed


def wait(prompt_id, timeout=900, poll=2.0):
    """Poll /history until the prompt finishes. Returns the history entry or None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            h = _get(f"/history/{prompt_id}")
            if prompt_id in h:
                return h[prompt_id]
        except Exception:
            pass
        time.sleep(poll)
    return None


def download_images(hist, dest: Path):
    """Pull every image out of a history entry into dest/. Returns [paths]."""
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for node in (hist or {}).get("outputs", {}).values():
        for img in node.get("images", []):
            q = urllib.parse.urlencode({
                "filename": img["filename"],
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
            })
            try:
                with urllib.request.urlopen(f"{COMFY}/view?{q}", timeout=60) as r:
                    data = r.read()
                p = dest / img["filename"]
                p.write_bytes(data)
                saved.append(p)
            except Exception as e:
                print(f"  download failed for {img.get('filename')}: {e}")
    return saved


def render_pack(pack_dir: Path, ckpt=DEFAULT_CKPT, steps=28, cfg=5.0,
                limit=None, aspect="16:9"):
    """Render every image_prompt in a content pack, one at a time."""
    brief_p = pack_dir / "brief.json"
    if not brief_p.exists():
        raise SystemExit(f"no brief.json in {pack_dir}")
    brief = json.loads(brief_p.read_text())
    prompts = brief.get("image_prompts") or []
    if limit:
        prompts = prompts[:limit]
    if not prompts:
        raise SystemExit("pack has no image_prompts")

    w, h = {"16:9": (1344, 768), "1:1": (1024, 1024), "9:16": (768, 1344),
            "4:3": (1152, 896)}.get(aspect, (1344, 768))
    neg = brief.get("negative") or DEFAULT_NEG
    dest = pack_dir / "images"
    manifest = []

    print(f"Rendering {len(prompts)} image(s) at {w}x{h}, {steps} steps, {ckpt}")
    for i, pr in enumerate(prompts, 1):
        print(f"[{i}/{len(prompts)}] {pr[:70]}...")
        pid, seed = queue(pr, neg, ckpt, w, h, steps, cfg)
        hist = wait(pid)
        if not hist:
            print("  TIMEOUT — skipping")
            manifest.append({"prompt": pr, "ok": False, "seed": seed})
            continue
        files = download_images(hist, dest)
        print(f"  -> {len(files)} file(s), seed {seed}")
        manifest.append({"prompt": pr, "ok": bool(files), "seed": seed,
                         "files": [f.name for f in files]})
        time.sleep(1)  # let memory settle between renders

    (pack_dir / "render_manifest.json").write_text(json.dumps({
        "checkpoint": ckpt, "steps": steps, "cfg": cfg, "size": [w, h],
        "rendered": manifest, "ts": int(time.time()),
    }, indent=2))
    ok = sum(1 for m in manifest if m["ok"])
    print(f"\nDONE: {ok}/{len(prompts)} rendered -> {dest}")
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", help="path to content_out/<slug>")
    ap.add_argument("--prompt", help="one-off prompt instead of a pack")
    ap.add_argument("--out", help="output dir for --prompt mode")
    ap.add_argument("--ckpt", default=DEFAULT_CKPT)
    ap.add_argument("--steps", type=int, default=28)
    ap.add_argument("--cfg", type=float, default=5.0)
    ap.add_argument("--aspect", default="16:9", choices=["16:9", "1:1", "9:16", "4:3"])
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()

    if not is_up():
        raise SystemExit(f"ComfyUI not reachable at {COMFY}")

    if a.prompt:
        w, h = {"16:9": (1344, 768), "1:1": (1024, 1024), "9:16": (768, 1344),
                "4:3": (1152, 896)}[a.aspect]
        pid, seed = queue(a.prompt, DEFAULT_NEG, a.ckpt, w, h, a.steps, a.cfg)
        print(f"queued {pid} seed={seed}")
        hist = wait(pid)
        files = download_images(hist, Path(a.out or "render_out"))
        print(f"saved: {[str(f) for f in files]}")
        return

    if not a.pack:
        raise SystemExit("need --pack or --prompt")
    render_pack(Path(a.pack), a.ckpt, a.steps, a.cfg, a.limit, a.aspect)


if __name__ == "__main__":
    main()
