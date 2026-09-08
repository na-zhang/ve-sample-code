"""
Seedance 2.5 视频生成 Demo
使用 asset_id 作为参考素材输入，支持单个或多个 asset ID
生成完成后视频自动上传到 TOS naz/output/ 目录

用法:
  # 批量模式：每个 asset 独立生成一个视频（全部作为 reference_video）
  python3 seedance_demo.py                                # 使用 .env 中的 ASSET_ID
  python3 seedance_demo.py asset-xxx asset-yyy            # 命令行传入多个 asset ID
  ASSET_IDS=asset-xxx,asset-yyy python3 seedance_demo.py  # 环境变量传入多个

  # 多素材模式：图片+视频混合参考，单次生成一个视频
  REFERENCE_VIDEO_IDS=asset-xxx  REFERENCE_IMAGE_IDS=asset-yyy python3 seedance_demo.py
"""

import os
import sys
import time
import json
import subprocess
import datetime
from dotenv import load_dotenv
from byteplussdkarkruntime import Ark
import requests
import tos

load_dotenv(override=True)

API_KEY = os.environ.get("ARK_API_KEY")
BASE_URL = os.environ.get("ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3")
MODEL_ID = os.environ.get("SEEDANCE_MODEL_ID", "ep-20260814153328-24djt")
PROMPT = os.environ.get("PROMPT")
VIDEO_RATIO = os.environ.get("VIDEO_RATIO", "9:16")
VIDEO_RESOLUTION = os.environ.get("VIDEO_RESOLUTION", "480p")
_dur_raw = os.environ.get("VIDEO_DURATION", "14")
try:
    VIDEO_DURATION = int(_dur_raw)
except (ValueError, TypeError):
    VIDEO_DURATION = 14
GENERATE_AUDIO = os.environ.get("GENERATE_AUDIO", "true").lower() == "true"
WATERMARK = os.environ.get("WATERMARK", "false").lower() == "true"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")

TOS_AK = os.environ.get("ARK_ACCESS_KEY")
TOS_SK = os.environ.get("ARK_SECRET_KEY")
TOS_TOKEN = os.environ.get("ARK_SESSION_TOKEN", "").strip().strip('"')
TOS_REGION = os.environ.get("ARK_REGION", "ap-southeast-1")
TOS_BUCKET = os.environ.get("TOS_BUCKET", "naz")
TOS_ENDPOINT = os.environ.get("TOS_ENDPOINT", "tos-ap-southeast-1.bytepluses.com")
TOS_OUTPUT_PREFIX = os.environ.get("TOS_OUTPUT_PREFIX", "output")
IS_STS = bool(TOS_TOKEN)


def _parse_asset_ids(raw):
    """将逗号分隔的字符串解析为 asset ID 列表"""
    if not raw:
        return []
    return [a.strip() for a in raw.split(",") if a.strip()]


def get_asset_ids():
    """批量模式：从命令行或环境变量获取 asset ID 列表"""
    if len(sys.argv) > 1:
        ids = []
        for arg in sys.argv[1:]:
            ids.extend(a.strip() for a in arg.split(",") if a.strip())
        return ids
    env_ids = os.environ.get("ASSET_IDS", "")
    if env_ids:
        return _parse_asset_ids(env_ids)
    single = os.environ.get("ASSET_ID", "")
    if single and single != "your_asset_id_here":
        return [single.strip()]
    return []


def get_reference_assets():
    """多素材模式：分别获取视频参考、图片参考、首帧、尾帧的 asset ID 列表"""
    video_ids = _parse_asset_ids(os.environ.get("REFERENCE_VIDEO_IDS", ""))
    image_ids = _parse_asset_ids(os.environ.get("REFERENCE_IMAGE_IDS", ""))
    first_frame_ids = _parse_asset_ids(os.environ.get("REFERENCE_FIRST_FRAME_IDS", ""))
    last_frame_ids = _parse_asset_ids(os.environ.get("REFERENCE_LAST_FRAME_IDS", ""))
    return video_ids, image_ids, first_frame_ids, last_frame_ids


def build_reference_content(prompt, video_ids, image_ids,
                            first_frame_ids=None, last_frame_ids=None):
    """根据视频/图片参考 ID 列表构建 content 列表，支持首尾帧"""
    content = [{"type": "text", "text": prompt}]
    for vid in video_ids:
        content.append({
            "type": "video_url",
            "video_url": {"url": f"asset://{vid}"},
            "role": "reference_video",
        })
    for iid in image_ids:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"asset://{iid}"},
            "role": "reference_image",
        })
    for fid in (first_frame_ids or []):
        content.append({
            "type": "image_url",
            "image_url": {"url": f"asset://{fid}"},
            "role": "first_frame",
        })
    for lid in (last_frame_ids or []):
        content.append({
            "type": "image_url",
            "image_url": {"url": f"asset://{lid}"},
            "role": "last_frame",
        })
    return content


def get_tos_client():
    return tos.TosClientV2(
        ak=TOS_AK,
        sk=TOS_SK,
        endpoint=TOS_ENDPOINT,
        region=TOS_REGION,
        security_token=TOS_TOKEN if IS_STS else None,
        connection_time=30,
        socket_timeout=120,
        max_retry_count=3,
    )


def probe_video(file_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,r_frame_rate:format=duration,bit_rate,size",
             "-of", "json", file_path],
            capture_output=True, text=True, timeout=10
        )
        info = json.loads(result.stdout)
        stream = info.get("streams", [{}])[0]
        fmt = info.get("format", {})
        w = int(stream.get("width", 0))
        h = int(stream.get("height", 0))
        dur = float(fmt.get("duration", 0))
        br = int(fmt.get("bit_rate", 0))
        sz = int(fmt.get("size", 0))
        fps_raw = stream.get("r_frame_rate", "0/1")
        try:
            num, den = fps_raw.split("/")
            fps = round(int(num) / int(den), 2) if int(den) else 0
        except Exception:
            fps = 0
        return {"width": w, "height": h, "duration": round(dur, 2), "bitrate": br,
                "size": sz, "fps": fps, "codec": stream.get("codec_name", "")}
    except Exception as e:
        return {"probe_error": str(e)}


def upload_to_tos_output(local_path, object_key):
    tos_client = get_tos_client()
    r = tos_client.put_object_from_file(
        bucket=TOS_BUCKET,
        key=object_key,
        file_path=local_path,
        content_type="video/mp4",
        acl=tos.ACLType.ACL_Public_Read,
    )
    return f"https://{TOS_BUCKET}.{TOS_ENDPOINT}/{object_key}"


def _run_task(client, tos_client, content, label, index, total):
    """核心任务执行：提交 -> 轮询 -> 下载 -> 上传 TOS"""
    start_time = time.time()
    print(f"\n{'=' * 70}")
    print(f"[{index}/{total}] {label}")
    print(f"{'=' * 70}")

    # 计算参考素材数量
    reference_count = len([c for c in content if c.get("type") != "text"])

    input_params = {
        "model": MODEL_ID,
        "prompt": PROMPT,
        "ratio": VIDEO_RATIO,
        "resolution": VIDEO_RESOLUTION,
        "duration": VIDEO_DURATION,
        "generate_audio": GENERATE_AUDIO,
        "watermark": WATERMARK,
    }
    if reference_count > 0:
        input_params["reference_count"] = reference_count

    print("\n--- INPUT PARAMS ---")
    print(json.dumps(input_params, indent=2, ensure_ascii=False))
    print("--- CONTENT ---")
    print(json.dumps(content, indent=2, ensure_ascii=False))

    create_kwargs = dict(
        model=MODEL_ID,
        content=content,
        generate_audio=GENERATE_AUDIO,
        ratio=VIDEO_RATIO,
        resolution=VIDEO_RESOLUTION,
        watermark=WATERMARK,
        duration=VIDEO_DURATION,
    )

    print("\n--- SUBMITTING TASK ---")
    t0 = time.time()
    create_result = client.content_generation.tasks.create(**create_kwargs)
    task_id = create_result.id
    print(f"Task ID:     {task_id}")
    print(f"Submit time: {datetime.datetime.now().isoformat()}")

    print("\n--- POLLING ---")
    while True:
        get_result = client.content_generation.tasks.get(task_id=task_id)
        status = get_result.status
        elapsed = int(time.time() - t0)
        print(f"  [{datetime.datetime.now().strftime('%H:%M:%S')}] status={status}, elapsed={elapsed}s")

        if status == "succeeded":
            gen_time = time.time() - t0
            video_url = get_result.content.video_url

            os.makedirs(OUTPUT_DIR, exist_ok=True)
            safe_label = label.replace(" ", "_").replace("/", "_").replace(",", "")
            local_filename = f"{index:03d}_{safe_label}.mp4"
            local_path = os.path.join(OUTPUT_DIR, local_filename)

            print(f"\n--- DOWNLOADING ---")
            print(f"Source URL: {video_url}")
            resp = requests.get(video_url, stream=True, timeout=300)
            resp.raise_for_status()
            total_size = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size:
                        pct = downloaded * 100 // total_size
                        print(f"\r  Progress: {pct}% ({downloaded // 1024}KB / {total_size // 1024}KB)", end="")
            print(f"\n  Local: {local_path} ({downloaded // 1024} KB)")

            meta = probe_video(local_path)

            print(f"\n--- UPLOADING TO TOS ---")
            tos_key = f"{TOS_OUTPUT_PREFIX}/{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{local_filename}"
            tos_url = upload_to_tos_output(local_path, tos_key)
            print(f"  TOS URL: {tos_url}")

            output_params = {
                "task_id": task_id,
                "status": "succeeded",
                "generation_time_s": round(gen_time, 1),
                "total_time_s": round(time.time() - start_time, 1),
                "local_path": os.path.abspath(local_path),
                "tos_url": tos_url,
                "tos_key": tos_key,
                "video_metadata": meta,
                "content": {
                    "video_url": video_url,
                },
                "usage": {
                    "token_count": getattr(get_result, 'usage', None) and getattr(get_result.usage, 'total_tokens', None),
                },
            }
            print("\n--- OUTPUT PARAMS ---")
            print(json.dumps(output_params, indent=2, ensure_ascii=False))

            return {**input_params, **output_params, "status": "success"}

        elif status == "failed":
            err = get_result.error
            output_params = {
                "task_id": task_id,
                "status": "failed",
                "error": str(err),
                "generation_time_s": round(time.time() - t0, 1),
            }
            print("\n--- OUTPUT PARAMS (FAILED) ---")
            print(json.dumps(output_params, indent=2, ensure_ascii=False))
            return {**input_params, **output_params}
        else:
            time.sleep(POLL_INTERVAL)


def process_single_asset(client, tos_client, asset_id, index, total):
    """批量模式：单个 asset 作为 reference_video 独立生成"""
    content = [
        {"type": "text", "text": PROMPT},
        {"type": "video_url", "video_url": {"url": f"asset://{asset_id}"}, "role": "reference_video"},
    ]
    result = _run_task(client, tos_client, content, asset_id, index, total)
    result["asset_id"] = asset_id
    return result


def process_with_references(client, tos_client, video_ids, image_ids,
                            first_frame_ids=None, last_frame_ids=None):
    """多素材模式：图片+视频+首尾帧混合参考，单次生成"""
    content = build_reference_content(PROMPT, video_ids, image_ids,
                                      first_frame_ids, last_frame_ids)
    label_parts = []
    if video_ids:
        label_parts.append(f"video_{len(video_ids)}")
    if image_ids:
        label_parts.append(f"image_{len(image_ids)}")
    if first_frame_ids:
        label_parts.append(f"first_{len(first_frame_ids)}")
    if last_frame_ids:
        label_parts.append(f"last_{len(last_frame_ids)}")
    label = "+".join(label_parts) if label_parts else "multi_ref"
    result = _run_task(client, tos_client, content, label, 1, 1)
    result["reference_video_ids"] = video_ids
    result["reference_image_ids"] = image_ids
    result["reference_first_frame_ids"] = first_frame_ids or []
    result["reference_last_frame_ids"] = last_frame_ids or []
    return result


def main():
    video_ref_ids, image_ref_ids, first_frame_ids, last_frame_ids = get_reference_assets()
    asset_ids = get_asset_ids()

    # 纯文本模式：无参考素材，仅用 prompt 生成
    is_text_only = not video_ref_ids and not image_ref_ids and not first_frame_ids and not last_frame_ids and not asset_ids

    if not API_KEY or API_KEY == "your_api_key_here":
        raise ValueError("请在 .env 中设置有效的 ARK_API_KEY")
    if not PROMPT:
        raise ValueError("请在 .env 中设置 PROMPT 文本提示词")

    # 纯文本模式：仅用 prompt 生成视频
    if is_text_only:
        client = Ark(base_url=BASE_URL, api_key=API_KEY)
        tos_client = get_tos_client()

        print("=" * 70)
        print("Seedance 2.5 视频生成 Demo (纯文本模式)")
        print(f"Prompt:      {PROMPT}")
        print(f"Model:       {MODEL_ID}")
        print(f"Ratio:       {VIDEO_RATIO}")
        print(f"Resolution:  {VIDEO_RESOLUTION}")
        print(f"Duration:    {VIDEO_DURATION}s")
        print(f"Audio:       {GENERATE_AUDIO}")
        print(f"Watermark:   {WATERMARK}")
        print(f"Output dir:  {OUTPUT_DIR}")
        print(f"TOS bucket:  {TOS_BUCKET}/{TOS_OUTPUT_PREFIX}/")
        print("=" * 70)

        content = [{"type": "text", "text": PROMPT}]
        try:
            r = _run_task(client, tos_client, content, "text_only", 1, 1)
        except Exception as e:
            print(f"\n❌ 纯文本生成异常: {e}")
            import traceback
            traceback.print_exc()
            r = {"status": "error", "error": str(e)}

        print(f"\n\n{'=' * 70}")
        status = "COMPLETE" if r.get("status") == "success" else "FAILED"
        print(f"TASK {status}")
        print(f"{'=' * 70}")
        icon = "✅" if r.get("status") == "success" else "❌"
        print(f"  {icon} Status: {r.get('status')}")
        if r.get("status") == "success":
            print(f"     Duration: {r.get('video_metadata', {}).get('duration', '?')}s, "
                  f"Resolution: {r.get('video_metadata', {}).get('width', '?')}x{r.get('video_metadata', {}).get('height', '?')}, "
                  f"Size: {r.get('video_metadata', {}).get('size', 0) // 1024}KB")
            print(f"     TOS: {r.get('tos_url', '')}")
            print(f"     Local: {r.get('local_path', '')}")
        else:
            print(f"     Error: {r.get('error', '')}")

        log_path = os.path.join(OUTPUT_DIR, f"text_only_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(r, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nLog saved to: {os.path.abspath(log_path)}")
        return

    # 多素材模式：VIDEO + IMAGE + 首尾帧 混合参考
    if video_ref_ids or image_ref_ids or first_frame_ids or last_frame_ids:
        client = Ark(base_url=BASE_URL, api_key=API_KEY)
        tos_client = get_tos_client()

        print("=" * 70)
        print("Seedance 2.5 视频生成 Demo (多素材模式)")
        print(f"Video refs:  {video_ref_ids if video_ref_ids else '(none)'}")
        print(f"Image refs:  {image_ref_ids if image_ref_ids else '(none)'}")
        print(f"First frame: {first_frame_ids if first_frame_ids else '(none)'}")
        print(f"Last frame:  {last_frame_ids if last_frame_ids else '(none)'}")
        print(f"Model:       {MODEL_ID}")
        print(f"Ratio:       {VIDEO_RATIO}")
        print(f"Resolution:  {VIDEO_RESOLUTION}")
        print(f"Duration:    {VIDEO_DURATION}s")
        print(f"Audio:       {GENERATE_AUDIO}")
        print(f"Watermark:   {WATERMARK}")
        print(f"Output dir:  {OUTPUT_DIR}")
        print(f"TOS bucket:  {TOS_BUCKET}/{TOS_OUTPUT_PREFIX}/")
        print("=" * 70)

        try:
            r = process_with_references(client, tos_client, video_ref_ids, image_ref_ids,
                                        first_frame_ids, last_frame_ids)
        except Exception as e:
            print(f"\n❌ 多素材处理异常: {e}")
            import traceback
            traceback.print_exc()
            r = {"status": "error", "error": str(e)}

        print(f"\n\n{'=' * 70}")
        status = "COMPLETE" if r.get("status") == "success" else "FAILED"
        print(f"TASK {status}")
        print(f"{'=' * 70}")
        icon = "✅" if r.get("status") == "success" else "❌"
        print(f"  {icon} Status: {r.get('status')}")
        if r.get("status") == "success":
            print(f"     Duration: {r.get('video_metadata', {}).get('duration', '?')}s, "
                  f"Resolution: {r.get('video_metadata', {}).get('width', '?')}x{r.get('video_metadata', {}).get('height', '?')}, "
                  f"Size: {r.get('video_metadata', {}).get('size', 0) // 1024}KB")
            print(f"     TOS: {r.get('tos_url', '')}")
            print(f"     Local: {r.get('local_path', '')}")
        else:
            print(f"     Error: {r.get('error', '')}")

        log_path = os.path.join(OUTPUT_DIR, f"multi_ref_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(r, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nLog saved to: {os.path.abspath(log_path)}")
        return

    # 批量模式（原有逻辑）
    client = Ark(base_url=BASE_URL, api_key=API_KEY)
    tos_client = get_tos_client()

    print("=" * 70)
    print("Seedance 2.5 视频生成 Demo (批量模式)")
    print(f"Assets:      {len(asset_ids)}")
    print(f"Model:       {MODEL_ID}")
    print(f"Ratio:       {VIDEO_RATIO}")
    print(f"Resolution:  {VIDEO_RESOLUTION}")
    print(f"Duration:    {VIDEO_DURATION}s")
    print(f"Audio:       {GENERATE_AUDIO}")
    print(f"Watermark:   {WATERMARK}")
    print(f"Output dir:  {OUTPUT_DIR}")
    print(f"TOS bucket:  {TOS_BUCKET}/{TOS_OUTPUT_PREFIX}/")
    print(f"TOS endpoint:{TOS_ENDPOINT}")
    print("=" * 70)

    results = []
    for i, aid in enumerate(asset_ids, 1):
        try:
            r = process_single_asset(client, tos_client, aid, i, len(asset_ids))
            results.append(r)
        except Exception as e:
            print(f"\n❌ Asset {aid} 处理异常: {e}")
            import traceback
            traceback.print_exc()
            results.append({"asset_id": aid, "status": "error", "error": str(e)})

    print(f"\n\n{'=' * 70}")
    print(f"BATCH COMPLETE - {sum(1 for r in results if r.get('status')=='success')}/{len(results)} succeeded")
    print(f"{'=' * 70}")
    for r in results:
        icon = "✅" if r.get("status") == "success" else "❌"
        print(f"  {icon} {r.get('asset_id','?')}")
        if r.get("status") == "success":
            print(f"     Duration: {r.get('video_metadata',{}).get('duration','?')}s, "
                  f"Resolution: {r.get('video_metadata',{}).get('width','?')}x{r.get('video_metadata',{}).get('height','?')}, "
                  f"Size: {r.get('video_metadata',{}).get('size',0)//1024}KB")
            print(f"     TOS: {r.get('tos_url','')}")
            print(f"     Local: {r.get('local_path','')}")
        else:
            print(f"     Error: {r.get('error','')}")

    log_path = os.path.join(OUTPUT_DIR, f"batch_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nFull log saved to: {os.path.abspath(log_path)}")


if __name__ == "__main__":
    main()