import os, sys, time
from dotenv import load_dotenv
load_dotenv(override=True)

from seedance_asset_upload import upload_asset, get_or_create_group
from byteplussdkarkruntime import Ark
import requests

API_KEY = os.environ.get("ARK_API_KEY")
BASE_URL = os.environ.get("ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3")
MODEL_ID = os.environ.get("SEEDANCE_MODEL_ID")
PROMPT = os.environ.get("PROMPT")
VIDEO_RATIO = os.environ.get("VIDEO_RATIO", "9:16")
VIDEO_RESOLUTION = os.environ.get("VIDEO_RESOLUTION", "480p")
GENERATE_AUDIO = os.environ.get("GENERATE_AUDIO", "true").lower() == "true"
WATERMARK = os.environ.get("WATERMARK", "false").lower() == "true"
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
_dur = os.environ.get("VIDEO_DURATION", "-1")
try:
    VIDEO_DURATION = int(_dur) if int(_dur) > 0 else None
except (ValueError, TypeError):
    VIDEO_DURATION = None

clip_dir = "clip"
clips = sorted(f for f in os.listdir(clip_dir) if f.endswith(".mp4"))
print(f"=== Found {len(clips)} clips in {clip_dir}/ ===")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(clip_dir, exist_ok=True)

print("Ensuring asset group exists...")
group_id = get_or_create_group()
print(f"Asset group: {group_id}\n")

client = Ark(base_url=BASE_URL, api_key=API_KEY)
results = []

for i, clip_name in enumerate(clips, 1):
    local_path = os.path.join(clip_dir, clip_name)
    print(f"\n{'#'*60}")
    print(f"# [{i}/{len(clips)}] {clip_name}")
    print(f"{'#'*60}")

    try:
        print("--- Uploading to asset library ---")
        gid, aid = upload_asset(local_path)
        print(f"Asset ID: {aid}")

        print("--- Generating video (remove watermark) ---")
        content = [
            {"type": "text", "text": PROMPT},
            {"type": "video_url", "video_url": {"url": f"asset://{aid}"}, "role": "reference_video"},
        ]
        kw = dict(model=MODEL_ID, content=content, generate_audio=GENERATE_AUDIO,
                  ratio=VIDEO_RATIO, resolution=VIDEO_RESOLUTION, watermark=WATERMARK)
        if VIDEO_DURATION is not None:
            kw["duration"] = VIDEO_DURATION

        task = client.content_generation.tasks.create(**kw)
        tid = task.id
        print(f"Task ID: {tid}")

        while True:
            gr = client.content_generation.tasks.get(task_id=tid)
            st = gr.status
            if st == "succeeded":
                vurl = gr.content.video_url
                out = os.path.join(OUTPUT_DIR, f"{i:03d}_{clip_name}")
                resp = requests.get(vurl, stream=True, timeout=300)
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                dl = 0
                with open(out, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                        dl += len(chunk)
                        if total:
                            print(f"\r  Downloading: {dl*100//total}%", end="")
                sz = os.path.getsize(out)
                print(f"\n  Saved: {out} ({sz//1024} KB)")
                results.append((clip_name, aid, tid, out))
                break
            elif st == "failed":
                print(f"  Task failed: {gr.error}")
                results.append((clip_name, aid, tid, f"FAILED: {gr.error}"))
                break
            else:
                print(f"  Status: {st} ...")
                time.sleep(10)

    except Exception as e:
        print(f"  ERROR: {e}")
        results.append((clip_name, None, None, f"ERROR: {e}"))

print(f"\n\n{'='*60}")
ok = sum(1 for _,_,_,r in results if os.path.exists(r))
print(f"BATCH COMPLETE: {ok}/{len(results)} succeeded")
print(f"{'='*60}")
for name, aid, tid, res in results:
    icon = "✅" if os.path.exists(res) else "❌"
    print(f"  {icon} {name}")
    if aid:
        print(f"     Asset: {aid}")
    if tid:
        print(f"     Task:  {tid}")
    print(f"     Result:{res}")