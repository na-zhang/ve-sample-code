"""
Seedance 资产上传 Demo
上传本地图片/视频到私有资产库，跳过内容审查 (Moderation Skip)
使用官方 TOS Python SDK 上传文件
所有资产默认归入 "basementgirl" 资产组（自动复用，不重复创建）
"""

import os
import time
import json
import hashlib
import hmac
import datetime
import urllib.parse
from dotenv import load_dotenv
import requests
import tos

load_dotenv()

# ============================================
# 从 .env 读取配置
# ============================================
AK = os.environ.get("ARK_ACCESS_KEY")
SK = os.environ.get("ARK_SECRET_KEY")
SESSION_TOKEN = os.environ.get("ARK_SESSION_TOKEN", "").strip().strip('"')
REGION = os.environ.get("ARK_REGION", "ap-southeast-1")
PROJECT_NAME = os.environ.get("ARK_PROJECT_NAME", "default")

LOCAL_FILE_PATH = os.environ.get("ASSET_LOCAL_FILE_PATH")
ASSET_TYPE = os.environ.get("ASSET_TYPE", "Image")
ASSET_NAME = os.environ.get("ASSET_NAME", "")
ASSET_GROUP_ID = os.environ.get("ASSET_GROUP_ID", "")
ASSET_GROUP_NAME = os.environ.get("ASSET_GROUP_NAME", "basementgirl")
ASSET_GROUP_DESC = os.environ.get("ASSET_GROUP_DESC", "Basement Girl asset group")
MODERATION_SKIP = os.environ.get("MODERATION_SKIP", "true").lower() == "true"

TOS_BUCKET = os.environ.get("TOS_BUCKET", "")
TOS_ENDPOINT = os.environ.get("TOS_ENDPOINT", "tos-ap-southeast-1.bytepluses.com")
TOS_UPLOAD_PREFIX = os.environ.get("TOS_UPLOAD_PREFIX", "seedance_assets").strip("/")

ARK_API_HOST = "ark.ap-southeast-1.byteplusapi.com"
IS_STS = bool(SESSION_TOKEN)
GROUP_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".asset_group_cache")


def validate_config():
    if not AK:
        raise ValueError("请在 .env 中设置 ARK_ACCESS_KEY")
    if not SK:
        raise ValueError("请在 .env 中设置 ARK_SECRET_KEY")
    if IS_STS:
        print(f"[INFO] 使用 STS 临时凭证，Session Token 长度: {len(SESSION_TOKEN)}")
    if LOCAL_FILE_PATH and not os.path.exists(LOCAL_FILE_PATH):
        raise ValueError(f"文件不存在: {LOCAL_FILE_PATH}")
    if LOCAL_FILE_PATH and ASSET_TYPE not in ("Image", "Video", "Audio"):
        raise ValueError(f"ASSET_TYPE 必须是 Image/Video/Audio，当前: {ASSET_TYPE}")


def _signing_key(sk, date_stamp, region, service):
    k_date = hmac.new(sk.encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(k_service, "request".encode("utf-8"), hashlib.sha256).digest()


def _canonical_query(query):
    res = []
    for key in sorted(query.keys()):
        value = str(query[key])
        res.append((urllib.parse.quote(key, safe='-_.~'), urllib.parse.quote(value, safe='-_.~')))
    return '&'.join(f'{k}={v}' for k, v in res)


def sign_request(method, host, path, query, body, service="ark"):
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
        signed_headers_map["x-security-token"] = SESSION_TOKEN

    host_for_sign = host
    if ":" in host_for_sign:
        port = host_for_sign.split(":")[1]
        if port in ("80", "443"):
            signed_headers_map["host"] = host_for_sign.split(":")[0]

    signed_str = "".join(f"{k}:{signed_headers_map[k]}\n" for k in sorted(signed_headers_map.keys()))
    signed_headers_string = ";".join(sorted(signed_headers_map.keys()))

    canonical_query = _canonical_query(query)
    canonical_request = "\n".join([
        method, path, canonical_query, signed_str, signed_headers_string, payload_hash,
    ])

    credential_scope = f"{date_stamp}/{REGION}/{service}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256", x_date, credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    signing_key = _signing_key(SK, date_stamp, REGION, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"HMAC-SHA256 Credential={AK}/{credential_scope}, "
        f"SignedHeaders={signed_headers_string}, "
        f"Signature={signature}"
    )

    result = {
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": authorization,
        "Content-Type": "application/json",
    }
    if IS_STS:
        result["X-Security-Token"] = SESSION_TOKEN

    return result


def call_ark_api(action, body, verbose=True):
    """调用 Ark API"""
    method = "POST"
    path = "/"
    query = {"Action": action, "Version": "2024-01-01"}

    headers = sign_request(method, ARK_API_HOST, path, query, body)
    url = f"https://{ARK_API_HOST}/?{urllib.parse.urlencode(query)}"

    if verbose:
        print(f"\n>>> {action}")
        print(f"Request: {json.dumps(body, indent=2, ensure_ascii=False)}")

    resp = requests.post(url, headers=headers, json=body, timeout=30)
    result = resp.json()

    if verbose:
        print(f"Response: {json.dumps(result, indent=2, ensure_ascii=False)}")

    if resp.status_code != 200:
        raise RuntimeError(f"API {action} failed: {resp.status_code} - {result}")
    if "ResponseMetadata" in result and "Error" in result.get("ResponseMetadata", {}):
        raise RuntimeError(f"API {action} error: {result['ResponseMetadata']['Error']}")

    return result.get("Result", result)


def _load_group_cache():
    if os.path.exists(GROUP_CACHE_FILE):
        try:
            with open(GROUP_CACHE_FILE, "r") as f:
                cache = json.load(f)
            name = cache.get("name")
            gid = cache.get("id")
            if name == ASSET_GROUP_NAME and gid:
                return gid
        except Exception:
            pass
    return None


def _save_group_cache(group_id):
    try:
        with open(GROUP_CACHE_FILE, "w") as f:
            json.dump({"name": ASSET_GROUP_NAME, "id": group_id, "project": PROJECT_NAME}, f)
    except Exception:
        pass


def get_or_create_group():
    """
    获取或创建资产组:
    1. 优先使用 ASSET_GROUP_ID 环境变量
    2. 其次使用本地缓存文件
    3. 最后调用 CreateAssetGroup 创建新组并缓存
    """
    if ASSET_GROUP_ID:
        print(f"使用指定资产组 ID: {ASSET_GROUP_ID}")
        return ASSET_GROUP_ID

    cached = _load_group_cache()
    if cached:
        print(f"使用缓存资产组: {cached} ({ASSET_GROUP_NAME})")
        return cached

    print(f"创建资产组: {ASSET_GROUP_NAME}")
    group_result = call_ark_api("CreateAssetGroup", {
        "Name": ASSET_GROUP_NAME,
        "Description": ASSET_GROUP_DESC,
        "ProjectName": PROJECT_NAME,
    }, verbose=False)
    group_id = group_result.get("Id")
    print(f"资产组 ID: {group_id}")
    _save_group_cache(group_id)
    return group_id


def upload_to_tos(local_path, object_key):
    """
    使用官方 TOS Python SDK 上传本地文件到 TOS，返回公开访问 URL
    支持 STS 临时凭证
    """
    if not TOS_BUCKET:
        raise ValueError("请在 .env 中设置 TOS_BUCKET（TOS 存储桶名称）")

    print(f"\n>>> 上传文件到 TOS: {local_path} -> {object_key}")

    client = tos.TosClientV2(
        ak=AK,
        sk=SK,
        endpoint=TOS_ENDPOINT,
        region=REGION,
        security_token=SESSION_TOKEN if IS_STS else None,
        connection_time=30,
        socket_timeout=120,
        max_retry_count=3,
    )

    file_size = os.path.getsize(local_path)
    content_type = "video/mp4" if local_path.lower().endswith(".mp4") else (
        "image/png" if local_path.lower().endswith(".png") else
        "image/jpeg" if local_path.lower().endswith((".jpg", ".jpeg")) else
        "application/octet-stream"
    )

    try:
        result = client.put_object_from_file(
            bucket=TOS_BUCKET,
            key=object_key,
            file_path=local_path,
            content_type=content_type,
            acl=tos.ACLType.ACL_Public_Read,
        )
        print(f"TOS upload OK (status={result.status_code}, request_id={result.request_id})")
    except tos.exceptions.TosClientError as e:
        raise RuntimeError(f"TOS client error: {e.message}, cause: {e.cause}")
    except tos.exceptions.TosServerError as e:
        raise RuntimeError(
            f"TOS server error: code={e.code}, message={e.message}, "
            f"request_id={e.request_id}, http_code={e.status_code}, ec={e.ec}"
        )

    public_url = f"https://{TOS_BUCKET}.{TOS_ENDPOINT}/{object_key}"
    print(f"File URL: {public_url} (size={file_size})")
    return public_url


def upload_asset(local_path, asset_type=None, asset_name=None):
    """
    上传单个文件到资产库，返回 (group_id, asset_id)
    复用 basementgirl 资产组，跳过审查
    """
    if not os.path.exists(local_path):
        raise ValueError(f"文件不存在: {local_path}")

    atype = asset_type or ASSET_TYPE
    if atype == "Video" or local_path.lower().endswith(".mp4"):
        atype = "Video"
    elif local_path.lower().endswith((".png", ".jpg", ".jpeg")):
        atype = "Image"

    group_id = get_or_create_group()

    object_key = f"{TOS_UPLOAD_PREFIX}/{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.path.basename(local_path)}"
    file_url = upload_to_tos(local_path, object_key)

    create_body = {
        "GroupId": group_id,
        "URL": file_url,
        "AssetType": atype,
        "ProjectName": PROJECT_NAME,
    }
    if asset_name or ASSET_NAME:
        create_body["Name"] = asset_name or ASSET_NAME

    if MODERATION_SKIP:
        create_body["Moderation"] = {"Strategy": "Skip"}
    else:
        create_body["Moderation"] = {"Strategy": "Default"}

    asset_result = call_ark_api("CreateAsset", create_body, verbose=False)
    asset_id = asset_result.get("Id")
    print(f"资产已创建: {asset_id}")

    for _ in range(60):
        info = call_ark_api("GetAsset", {"Id": asset_id, "ProjectName": PROJECT_NAME}, verbose=False)
        st = info.get("Status", "Unknown")
        if st == "Active":
            print(f"资产已激活: {asset_id}")
            return group_id, asset_id
        elif st == "Failed":
            raise RuntimeError(f"资产处理失败: {info}")
        time.sleep(5)

    raise RuntimeError(f"资产处理超时，ID: {asset_id}")


def main():
    validate_config()

    print("=" * 60)
    print("Seedance 资产上传 Demo")
    print("=" * 60)
    print(f"Auth mode:   {'STS' if IS_STS else 'AK/SK'}")
    print(f"Local file:  {LOCAL_FILE_PATH}")
    print(f"Asset type:  {ASSET_TYPE}")
    print(f"Asset name:  {ASSET_NAME or '(auto)'}")
    print(f"Group name:  {ASSET_GROUP_NAME}")
    print(f"Moderation:  {'Skip' if MODERATION_SKIP else 'Default'}")
    print(f"TOS bucket:  {TOS_BUCKET or '(manual URL)'}")
    print(f"TOS endpoint:{TOS_ENDPOINT}")
    print("=" * 60)

    group_id = get_or_create_group()
    print(f"\n资产组 ID: {group_id}")

    print("\n" + "=" * 60)
    print("Step: 上传文件获取 URL")
    print("=" * 60)
    object_key = f"{TOS_UPLOAD_PREFIX}/{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.path.basename(LOCAL_FILE_PATH)}"

    if TOS_BUCKET:
        file_url = upload_to_tos(LOCAL_FILE_PATH, object_key)
    else:
        file_url = os.environ.get("ASSET_FILE_URL")
        if not file_url:
            raise ValueError(
                "请在 .env 中设置 TOS_BUCKET（自动上传到 TOS）\n"
                "或设置 ASSET_FILE_URL（手动提供文件公开 URL）"
            )
        print(f"使用提供的 URL: {file_url}")

    print("\n" + "=" * 60)
    print("Step: 创建资产")
    print("=" * 60)
    create_body = {
        "GroupId": group_id,
        "URL": file_url,
        "AssetType": ASSET_TYPE,
        "ProjectName": PROJECT_NAME,
    }
    if ASSET_NAME:
        create_body["Name"] = ASSET_NAME

    if MODERATION_SKIP:
        create_body["Moderation"] = {"Strategy": "Skip"}
    else:
        create_body["Moderation"] = {"Strategy": "Default"}

    asset_result = call_ark_api("CreateAsset", create_body)
    asset_id = asset_result.get("Id")
    print(f"\n资产 ID: {asset_id}")

    print("\n" + "=" * 60)
    print("Step: 轮询资产状态")
    print("=" * 60)
    poll_interval = 5
    max_attempts = 60
    for attempt in range(1, max_attempts + 1):
        asset_info = call_ark_api("GetAsset", {
            "Id": asset_id,
            "ProjectName": PROJECT_NAME,
        })
        status = asset_info.get("Status", "Unknown")
        print(f"  [{attempt}/{max_attempts}] Status: {status}")

        if status == "Active":
            print("\n" + "=" * 60)
            print("资产上传成功!")
            print("=" * 60)
            print(f"Asset Group ID: {group_id}")
            print(f"Asset ID:       {asset_id}")
            print(f"Asset Type:     {asset_info.get('AssetType')}")
            print(f"Asset URL:      {asset_info.get('URL')}")
            print(f"Moderation:     {asset_info.get('Moderation')}")
            print(f"Create Time:    {asset_info.get('CreateTime')}")
            return
        elif status == "Failed":
            raise RuntimeError(f"资产处理失败: {asset_info}")

        time.sleep(poll_interval)

    raise RuntimeError(f"资产处理超时（{max_attempts * poll_interval}s），请稍后查询资产 ID: {asset_id}")


if __name__ == "__main__":
    main()