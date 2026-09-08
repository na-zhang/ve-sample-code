"""
Seedance 2.5 视频编辑 Demo
基于官方文档: https://docs.volcengine.com/docs/82379/2607688
使用 omni_reference_task_type=edit 进行视频编辑（替换主体、增删对象、局部重绘等）
复用 .env 环境变量，支持命令行传入 asset ID

用法:
  python3 seedance_video_edit.py video_asset_id [image_asset_id ...]
  python3 seedance_video_edit.py                                          # 使用 .env 中的 SEEDANCE_EDIT_VIDEO_ASSET 等
  SEEDANCE_EDIT_VIDEO_ASSET=asset-xxx python3 seedance_video_edit.py       # 环境变量传入
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

# ============================================
# 从 .env 读取配置（复用现有变量 + 新增变量）
# ============================================
API_KEY = os.environ.get("ARK_API_KEY")
BASE_URL = os.environ.get("ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3")
MODEL_ID = os.environ.get("SEEDANCE_MODEL_ID", "ep-20260813140008-rnq5k")
PROMPT = os.environ.get("PROMPT")
VIDEO_RATIO = os.environ.get("VIDEO_RATIO", "adaptive")  # 视频编辑必须是 adaptive
VIDEO_RESOLUTION = os.environ.get("VIDEO_RESOLUTION", "720p")
GENERATE_AUDIO = os.environ.get("GENERATE_AUDIO", "false").lower() == "true"
WATERMARK = os.environ.get("WATERMARK", "false").lower() == "true"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")

# 视频编辑专用配置
OUTPUT_FORMAT = os.environ.get("SEEDANCE_OUTPUT_FORMAT", "mp4")  # mp4 或 mov
RETURN_LAST_FRAME = os.environ.get("SEEDANCE_RETURN_LAST_FRAME", "false").lower() == "true"

# 默认素材 ID（视频编辑至少需要一个 reference_video 作为待编辑视频）
EDIT_VIDEO_ASSET = os.environ.get("SEEDANCE_EDIT_VIDEO_ASSET", "")
EDIT_IMAGE_ASSETS = os.environ.get("SEEDANCE_EDIT_IMAGE_ASSETS", "")  # 逗号分隔，可选的多张参考图
EDIT_AUDIO_ASSETS = os.environ.get("SEEDANCE_EDIT_AUDIO_ASSETS", "")  # 逗号分隔，可选的多段参考音频

# TOS 配置
TOS_AK = os.environ.get("ARK_ACCESS_KEY")
TOS_SK = os.environ.get("ARK_SECRET_KEY")
TOS_TOKEN = os.environ.get("ARK_SESSION_TOKEN", "").strip().strip('"')
TOS_REGION = os.environ.get("ARK_REGION", "ap-southeast-1")
TOS_BUCKET = os.environ.get("TOS_BUCKET", "naz")
TOS_ENDPOINT = os.environ.get("TOS_ENDPOINT", "tos-ap-southeast-1.bytepluses.com")
TOS_OUTPUT_PREFIX = os.environ.get("TOS_OUTPUT_PREFIX", "output")
IS_STS = bool(TOS_TOKEN)


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


def parse_asset_ids():
    """
    解析命令行参数和环境变量中的 asset ID.
    返回 (video_asset_id, image_asset_ids, audio_asset_ids)
    命令行格式: python3 seedance_video_edit.py <video_asset> [image_asset1 image_asset2 ...]
    """
    video_id = EDIT_VIDEO_ASSET
    image_ids = [a.strip() for a in EDIT_IMAGE_ASSETS.split(",") if a.strip()] if EDIT_IMAGE_ASSETS else []
    audio_ids = [a.strip() for a in EDIT_AUDIO_ASSETS.split(",") if a.strip()] if EDIT_AUDIO_ASSETS else []

    if len(sys.argv) > 1:
        video_id = sys.argv[1]
        image_ids = []
        audio_ids = []
        for arg in sys.argv[2:]:
            # 简单判断：asset-xxx 格式，如果有多个传入作为参考图
            image_ids.append(arg.strip())

    return video_id, image_ids, audio_ids


def build_content(video_asset_id, image_asset_ids, audio_asset_ids):
    """
    构建视频编辑的 content 数组。
    根据官方文档:
    - 至少包含一个 role=reference_video 的参考视频
    - 可选 role=reference_image 的参考图片
    - 可选 role=reference_audio 的参考音频
    - 提示词中需包含编辑关键词: 编辑视频/增加/去掉/删除/修改/替换/改成
    """
    content = []

    # 文本提示词
    if PROMPT:
        content.append({"type": "text", "text": PROMPT})

    # 待编辑视频 (reference_video)
    if video_asset_id:
        content.append({
            "type": "video_url",
            "video_url": {"url": f"asset://{video_asset_id}"},
            "role": "reference_video",
        })

    # 参考图片 (reference_image)
    for img_id in image_asset_ids:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"asset://{img_id}"},
            "role": "reference_image",
        })

    # 参考音频 (reference_audio)
    for aud_id in audio_asset_ids:
        content.append({
            "type": "audio_url",
            "audio_url": {"url": f"asset://{aud_id}"},
            "role": "reference_audio",
        })

    return content


def run_video_edit(client, tos_client, video_asset_id, image_asset_ids, audio_asset_ids):
    start_time = time.time()
    print(f"\n{'=' * 70}")
    print(f"Seedance 2.5 视频编辑")
    print(f"待编辑视频: {video_asset_id}")
    if image_asset_ids:
        print(f"参考图片:    {', '.join(image_asset_ids)}")
    if audio_asset_ids:
        print(f"参考音频:    {', '.join(audio_asset_ids)}")
    print(f"{'=' * 70}")

    content = build_content(video_asset_id, image_asset_ids, audio_asset_ids)

    request_body = {
        "model": MODEL_ID,
        "content": content,
        "omni_reference_task_type": "edit",
        "ratio": "adaptive",
        "duration": -1,
        "resolution": VIDEO_RESOLUTION,
        "generate_audio": GENERATE_AUDIO,
        "watermark": WATERMARK,
        "output_format": OUTPUT_FORMAT,
        "return_last_frame": RETURN_LAST_FRAME,
    }

    input_params = {
        "task_type": "视频编辑 (video_edit)",
        **request_body,
    }
    print("\n--- INPUT PARAMS ---")
    print(json.dumps(input_params, indent=2, ensure_ascii=False))

    print("\n--- SUBMITTING TASK ---")
    t0 = time.time()

    # 使用 HTTP API 直接调用，因为 SDK 的 create() 不支持 omni_reference_task_type 参数
    api_url = f"{BASE_URL}/contents/generations/tasks"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.post(api_url, headers=headers, json=request_body, timeout=60)
    create_result = resp.json()
    task_id = create_result.get("id")
    print(f"Create response: {json.dumps(create_result, indent=2, ensure_ascii=False)}")
    print(f"Task ID:     {task_id}")
    print(f"Submit time: {datetime.datetime.now().isoformat()}")

    if resp.status_code != 200 or not task_id:
        raise RuntimeError(f"创建任务失败: {create_result}")

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
            ext = OUTPUT_FORMAT
            local_filename = f"edit_{video_asset_id}.{ext}"
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
                "content": {"video_url": video_url},
                "usage": {
                    "token_count": getattr(get_result, 'usage', None) and getattr(get_result.usage, 'total_tokens', None),
                },
            }
            print("\n--- OUTPUT PARAMS ---")
            print(json.dumps(output_params, indent=2, ensure_ascii=False))
            return output_params

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
            return output_params
        else:
            time.sleep(POLL_INTERVAL)


def main():
    video_asset_id, image_asset_ids, audio_asset_ids = parse_asset_ids()

    if not video_asset_id:
        raise ValueError(
            "请通过以下方式传入待编辑视频的 asset ID:\n"
            "  1. 命令行参数: python3 seedance_video_edit.py asset-xxx\n"
            "  2. 环境变量 SEEDANCE_EDIT_VIDEO_ASSET: 在 .env 中设置\n"
            "  3. 命令行也可以指定参考图: python3 seedance_video_edit.py video_asset image_asset1 image_asset2"
        )
    if not API_KEY or API_KEY == "your_api_key_here":
        raise ValueError("请在 .env 中设置有效的 ARK_API_KEY")
    if not PROMPT:
        raise ValueError("请在 .env 中设置 PROMPT 文本提示词")

    client = Ark(base_url=BASE_URL, api_key=API_KEY)
    tos_client = get_tos_client()

    print("=" * 70)
    print("Seedance 2.5 视频编辑")
    print(f"Model:       {MODEL_ID}")
    print(f"Resolution:  {VIDEO_RESOLUTION}")
    print(f"Audio:       {GENERATE_AUDIO}")
    print(f"Watermark:   {WATERMARK}")
    print(f"Format:      {OUTPUT_FORMAT}")
    print(f"Output dir:  {OUTPUT_DIR}")
    print(f"TOS bucket:  {TOS_BUCKET}/{TOS_OUTPUT_PREFIX}/")
    print("=" * 70)

    result = run_video_edit(client, tos_client, video_asset_id, image_asset_ids, audio_asset_ids)

    if result.get("status") == "succeeded":
        print(f"\n{'=' * 70}")
        print("视频编辑完成!")
        print(f"Local: {result.get('local_path')}")
        print(f"TOS:   {result.get('tos_url')}")
        print(f"{'=' * 70}")
    else:
        print(f"\n{'=' * 70}")
        print(f"视频编辑失败: {result.get('error')}")
        print(f"{'=' * 70}")


if __name__ == "__main__":
    main()