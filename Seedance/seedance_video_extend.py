"""
Seedance 2.5 视频延长 Demo
基于官方文档: https://docs.byteplus.com/en/docs/ModelArk/1520757
使用 omni_reference_task_type=extend 进行视频延长（向前/向后延长）
复用 .env 环境变量，支持命令行传入 asset ID

约束:
  - extend 任务不传 resolution 参数（自动跟随输入视频）
  - duration 为总输出时长（原始时长 + 延长时长），范围 4-30s
  - 提示词必须包含"向后延长"或"向前延长"等关键词
  - 含真人的视频须使用同账号近 30 天内受信任的模型原始产物，
    或平台认可的已授权真人素材；Moderation Skip 不等同于人像授权

用法:
  # 使用 asset ID 延长视频
  EXTEND_DURATION=26 python3 seedance_video_extend.py asset-xxx

  # 指定延长时长
  EXTEND_DURATION=18 python3 seedance_video_extend.py asset-xxx

环境变量:
  EXTEND_VIDEO_ASSET      - 待延长的视频 asset ID
  EXTEND_VIDEO_PATH       - 待延长的本地视频路径
  EXTEND_REFERENCE_VIDEO_ASSETS - 附加参考视频 asset ID，多个用逗号分隔
  EXTEND_DURATION         - 总输出时长（秒），默认 6
  EXTEND_GROUP_NAME       - 素材组名称，默认 carddeal
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
# 从 .env 读取配置
# ============================================
API_KEY = os.environ.get("ARK_API_KEY")
BASE_URL = os.environ.get("ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3")
MODEL_ID = os.environ.get("SEEDANCE_MODEL_ID", "ep-20260813140008-rnq5k")
PROMPT = os.environ.get("PROMPT")
VIDEO_RESOLUTION = os.environ.get("VIDEO_RESOLUTION", "720p")
GENERATE_AUDIO = os.environ.get("GENERATE_AUDIO", "false").lower() == "true"
WATERMARK = os.environ.get("WATERMARK", "false").lower() == "true"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "sd2.5_extend")

# 视频延长专用配置
EXTEND_DURATION = int(os.environ.get("EXTEND_DURATION", "6"))
EXTEND_VIDEO_ASSET = os.environ.get("EXTEND_VIDEO_ASSET", "")
EXTEND_VIDEO_PATH = os.environ.get("EXTEND_VIDEO_PATH", "")
EXTEND_REFERENCE_VIDEO_ASSETS = [
    asset_id.strip()
    for asset_id in os.environ.get("EXTEND_REFERENCE_VIDEO_ASSETS", "").split(",")
    if asset_id.strip()
]
EXTEND_GROUP_NAME = os.environ.get("EXTEND_GROUP_NAME", "carddeal")
EXTEND_GROUP_DESC = os.environ.get("EXTEND_GROUP_DESC", "Video extension asset group")

# TOS 配置
TOS_AK = os.environ.get("ARK_ACCESS_KEY")
TOS_SK = os.environ.get("ARK_SECRET_KEY")
TOS_TOKEN = os.environ.get("ARK_SESSION_TOKEN", "").strip().strip('"')
TOS_REGION = os.environ.get("ARK_REGION", "ap-southeast-1")
TOS_BUCKET = os.environ.get("TOS_BUCKET", "naz")
TOS_ENDPOINT = os.environ.get("TOS_ENDPOINT", "tos-ap-southeast-1.bytepluses.com")
TOS_UPLOAD_PREFIX = os.environ.get("TOS_UPLOAD_PREFIX", "naz/carddeal")
TOS_OUTPUT_PREFIX = os.environ.get("TOS_OUTPUT_PREFIX", "output")
IS_STS = bool(TOS_TOKEN)

# Ark API 资源管理配置（注意：OpenAPI 主机域名为 byteplusapi.com，不是 bytepluses.com）
ARK_API_HOST = os.environ.get("ARK_API_HOST", "ark.ap-southeast-1.byteplusapi.com")
PROJECT_NAME = os.environ.get("ARK_PROJECT_NAME", "default")
GROUP_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".extend_asset_group_cache")


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
    tos_client.put_object_from_file(
        bucket=TOS_BUCKET,
        key=object_key,
        file_path=local_path,
        content_type="video/mp4",
        acl=tos.ACLType.ACL_Public_Read,
    )
    return f"https://{TOS_BUCKET}.{TOS_ENDPOINT}/{object_key}"


def _sign_request(method, host, path, query, body, service="ark"):
    import hashlib
    import hmac
    import urllib.parse

    now = datetime.datetime.utcnow()
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    body_str = json.dumps(body) if body else ""
    payload_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()

    signed_headers_map = {
        "content-type": "application/json",
        "host": host,
        "x-content-sha256": payload_hash,
        "x-date": x_date,
    }
    if IS_STS:
        signed_headers_map["x-security-token"] = TOS_TOKEN

    signed_str = "".join(f"{k}:{signed_headers_map[k]}\n" for k in sorted(signed_headers_map.keys()))
    signed_headers_string = ";".join(sorted(signed_headers_map.keys()))

    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}"
        for k, v in sorted(query.items())
    )

    canonical_request = (
        f"{method}\n{path}\n{canonical_query}\n"
        f"{signed_str}\n{signed_headers_string}\n{payload_hash}"
    )

    def _signing_key(sk, ds, region, svc):
        k_date = hmac.new(sk.encode("utf-8"), ds.encode("utf-8"), hashlib.sha256).digest()
        k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
        k_service = hmac.new(k_region, svc.encode("utf-8"), hashlib.sha256).digest()
        return hmac.new(k_service, "request".encode("utf-8"), hashlib.sha256).digest()

    algorithm = "HMAC-SHA256"
    credential_scope = f"{date_stamp}/{TOS_REGION}/{service}/request"
    string_to_sign = (
        f"{algorithm}\n{x_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signing_key = _signing_key(TOS_SK, date_stamp, TOS_REGION, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{algorithm} Credential={TOS_AK}/{credential_scope}, "
        f"SignedHeaders={signed_headers_string}, Signature={signature}"
    )
    headers = {
        "Content-Type": "application/json",
        "Host": host,
        "X-Content-Sha256": payload_hash,
        "X-Date": x_date,
        "Authorization": authorization,
    }
    if IS_STS:
        headers["X-Security-Token"] = TOS_TOKEN
    return headers


def call_ark_api(action, body, verbose=True):
    """调用 Ark 资源管理 API（用于创建/查询资产组和资产）"""
    path = "/"
    query = {"Action": action, "Version": "2024-01-01"}
    headers = _sign_request("POST", ARK_API_HOST, path, query, body)
    url = f"https://{ARK_API_HOST}/?{'&'.join(f'{k}={v}' for k, v in query.items())}"

    if verbose:
        print(f"\n>>> {action}")
        print(f"Request: {json.dumps(body, indent=2, ensure_ascii=False)}")

    resp = requests.post(url, headers=headers, json=body, timeout=30)
    result = resp.json()
    if verbose:
        print(f"Response: {json.dumps(result, indent=2, ensure_ascii=False)}")
    if resp.status_code != 200:
        raise RuntimeError(f"API {action} failed: {resp.status_code} - {result}")
    if "Error" in (result.get("ResponseMetadata", {}) or {}):
        raise RuntimeError(f"API Error: {result['ResponseMetadata']['Error']}")
    return result.get("Result", {})


def _save_group_cache(group_id):
    try:
        with open(GROUP_CACHE_FILE, "w") as f:
            json.dump({"group_id": group_id, "group_name": EXTEND_GROUP_NAME}, f)
    except Exception:
        pass


def _load_group_cache():
    try:
        if os.path.exists(GROUP_CACHE_FILE):
            with open(GROUP_CACHE_FILE, "r") as f:
                data = json.load(f)
                if data.get("group_name") == EXTEND_GROUP_NAME:
                    return data.get("group_id")
    except Exception:
        pass
    return None


def get_or_create_group():
    # 与 seedance_asset_upload.py 对齐：直接 CreateAssetGroup（同名组可重复创建，
    # 组仅用于素材归类，不影响生成），组 ID 缓存到本地避免重复创建。
    cached = _load_group_cache()
    if cached:
        print(f"使用缓存资产组: {cached} ({EXTEND_GROUP_NAME})")
        return cached

    group_result = call_ark_api("CreateAssetGroup", {
        "Name": EXTEND_GROUP_NAME,
        "Description": EXTEND_GROUP_DESC,
        "ProjectName": PROJECT_NAME,
    }, verbose=False)
    group_id = group_result.get("Id")
    print(f"资产组已创建: {group_id} ({EXTEND_GROUP_NAME})")
    _save_group_cache(group_id)
    return group_id


def upload_video_to_asset(local_path, asset_name=None):
    """上传本地视频到素材库，返回 asset ID；不授予真人素材使用权限。"""
    print(f"\n>>> 上传视频到素材库: {local_path}")
    print("  注意: 本地上传的含真人脸视频不具备 Seedance 生成谱系，")
    print("        用于 extend 时可能被 InputVideoSensitiveContentDetected 拒绝。")
    group_id = get_or_create_group()

    object_key = f"{TOS_UPLOAD_PREFIX}/{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.path.basename(local_path)}"
    tos_client = get_tos_client()

    tos_client.put_object_from_file(
        bucket=TOS_BUCKET,
        key=object_key,
        file_path=local_path,
        content_type="video/mp4",
        acl=tos.ACLType.ACL_Public_Read,
    )
    file_url = f"https://{TOS_BUCKET}.{TOS_ENDPOINT}/{object_key}"
    print(f"  TOS URL: {file_url}")

    create_body = {
        "GroupId": group_id,
        "URL": file_url,
        "AssetType": "Video",
        "ProjectName": PROJECT_NAME,
        "Moderation": {"Strategy": "Skip"},
    }
    if asset_name:
        create_body["Name"] = asset_name

    asset_result = call_ark_api("CreateAsset", create_body, verbose=False)
    asset_id = asset_result.get("Id")
    print(f"  资产 ID: {asset_id}")

    for attempt in range(1, 61):
        info = call_ark_api("GetAsset", {"Id": asset_id, "ProjectName": PROJECT_NAME}, verbose=False)
        st = info.get("Status", "Unknown")
        if st == "Active":
            print(f"  资产已激活: {asset_id}")
            return asset_id
        elif st == "Failed":
            raise RuntimeError(f"资产处理失败: {info}")
        time.sleep(5)

    raise RuntimeError(f"资产处理超时，ID: {asset_id}")


def _failure_hint(raw_error):
    """根据服务端真实错误码给出可操作的排查提示（SDK 只透出笼统 InvalidParameter）。"""
    if not raw_error:
        return None
    code = str(raw_error.get("code", "")) if isinstance(raw_error, dict) else ""
    message = str(raw_error.get("message", "")) if isinstance(raw_error, dict) else str(raw_error)
    if "InputVideoSensitiveContentDetected" in code or "PrivacyInformation" in code or "real person" in message.lower():
        return (
            "输入视频含真人且未被平台信任：本地上传/外部 URL 的含人脸视频不能直接用于延长。"
            "请改用本账号近 30 天内由 Seedance 模型生成的原始产物 asset、预置虚拟人像，"
            "或平台认可的已授权真人素材；素材库 Moderation Skip 不等于真人肖像授权。"
        )
    return None


def run_video_extend(client, video_asset_id, reference_video_asset_ids=None):
    """执行视频延长：omni_reference_task_type=extend"""
    start_time = time.time()
    reference_video_asset_ids = reference_video_asset_ids or []

    # 第一个视频是待延长源，后续视频是提示词中 @video2、@video3... 的参考素材。
    content = [{"type": "text", "text": PROMPT}]
    content.append({
        "type": "video_url",
        "video_url": {"url": f"asset://{video_asset_id}"},
        "role": "reference_video",
    })
    for asset_id in reference_video_asset_ids:
        content.append({
            "type": "video_url",
            "video_url": {"url": f"asset://{asset_id}"},
            "role": "reference_video",
        })

    # extend 任务强校验：ratio 必须为 adaptive
    # 注意：extend 任务自动跟随输入视频分辨率，不传 resolution 参数
    request_body = {
        "model": MODEL_ID,
        "content": content,
        "omni_reference_task_type": "extend",
        "ratio": "adaptive",
        "duration": EXTEND_DURATION,
        "generate_audio": GENERATE_AUDIO,
        "watermark": WATERMARK,
    }

    input_params = {
        "task_type": "视频延长 (video_extend)",
        **request_body,
    }
    print(f"\n{'=' * 70}")
    print(f"Seedance 2.5 视频延长")
    print(f"待延长视频: {video_asset_id}")
    print(f"参考视频:   {reference_video_asset_ids or '(无)'}")
    print(f"输出总时长: {EXTEND_DURATION}s")
    print(f"{'=' * 70}")
    print("\n--- INPUT PARAMS ---")
    print(json.dumps(input_params, indent=2, ensure_ascii=False))
    print("--- CONTENT ---")
    print(json.dumps(content, indent=2, ensure_ascii=False))

    print("\n--- SUBMITTING TASK ---")
    t0 = time.time()

    # 使用 HTTP API 直接调用（SDK 不支持 omni_reference_task_type 参数）
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
            local_filename = f"extend_{video_asset_id}_{task_id}.mp4"
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
                "video_asset_id": video_asset_id,
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
            # SDK 的 error 通常只有笼统的 InvalidParameter，需拉取任务原始响应
            # 才能看到服务端真实错误码（如 InputVideoSensitiveContentDetected.PrivacyInformation）
            raw_error = None
            try:
                raw_resp = requests.get(
                    f"{api_url}/{task_id}",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    timeout=30,
                )
                raw_error = raw_resp.json().get("error")
            except Exception as fetch_err:
                raw_error = {"fetch_error": str(fetch_err)}

            output_params = {
                "task_id": task_id,
                "status": "failed",
                "error": str(err),
                "raw_error": raw_error,
                "generation_time_s": round(time.time() - t0, 1),
            }
            print("\n--- OUTPUT PARAMS (FAILED) ---")
            print(json.dumps(output_params, indent=2, ensure_ascii=False))

            hint = _failure_hint(raw_error) or _failure_hint({"message": str(err)})
            if hint:
                print(f"\n[失败原因提示] {hint}")
            return output_params
        else:
            time.sleep(POLL_INTERVAL)


def parse_args():
    """解析命令行参数，返回 video_asset_id"""
    video_asset_id = EXTEND_VIDEO_ASSET
    video_path = EXTEND_VIDEO_PATH

    for arg in sys.argv[1:]:
        if arg.startswith("asset-"):
            video_asset_id = arg
        elif os.path.exists(arg):
            video_path = arg

    return video_asset_id, video_path


def main():
    video_asset_id, video_path = parse_args()

    if not video_asset_id and not video_path:
        raise ValueError(
            "请通过以下方式传入待延长的视频:\n"
            "  1. 命令行: python3 seedance_video_extend.py asset-xxx\n"
            "  2. 命令行: python3 seedance_video_extend.py /path/to/video.mp4\n"
            "  3. 环境变量: EXTEND_VIDEO_ASSET=asset-xxx\n"
            "  4. 环境变量: EXTEND_VIDEO_PATH=/path/to/video.mp4"
        )
    if not API_KEY or API_KEY == "your_api_key_here":
        raise ValueError("请在 .env 中设置有效的 ARK_API_KEY")
    if not PROMPT:
        raise ValueError("请在 .env 中设置 PROMPT 文本提示词")

    # 如果是本地视频路径，先上传到素材库
    if not video_asset_id and video_path:
        if not os.path.exists(video_path):
            raise ValueError(f"视频文件不存在: {video_path}")
        video_asset_id = upload_video_to_asset(video_path, os.path.basename(video_path))

    print("=" * 70)
    print("Seedance 2.5 视频延长")
    print(f"Model:        {MODEL_ID}")
    print(f"待延长视频:   {video_asset_id}")
    print(f"输出总时长:   {EXTEND_DURATION}s")
    print(f"Resolution:   {VIDEO_RESOLUTION}")
    print(f"Audio:        {GENERATE_AUDIO}")
    print(f"Output dir:   {OUTPUT_DIR}")
    print("=" * 70)

    client = Ark(base_url=BASE_URL, api_key=API_KEY)

    result = run_video_extend(
        client,
        video_asset_id,
        EXTEND_REFERENCE_VIDEO_ASSETS,
    )

    if result.get("status") == "succeeded":
        print(f"\n{'=' * 70}")
        print("视频延长完成!")
        print(f"Local:    {result.get('local_path')}")
        print(f"TOS:      {result.get('tos_url')}")
        print(f"Duration: {result.get('video_metadata', {}).get('duration', '?')}s")
        print(f"{'=' * 70}")
    else:
        print(f"\n{'=' * 70}")
        print(f"视频延长失败: {result.get('error')}")
        print(f"{'=' * 70}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
