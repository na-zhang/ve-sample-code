"""批量上传 card01_6s.mp4 和 card02_extension.mp4 到素材库"""
import os
import sys
from dotenv import load_dotenv

load_dotenv(override=True)

# 将当前目录加入 path 以便导入 seedance_asset_upload
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seedance_asset_upload import upload_asset

FILES = [
    "/Users/bytedance/Documents/Code/Seedance/sd2.5_extend/card01_6s.mp4",
    "/Users/bytedance/Documents/Code/Seedance/sd2.5_extend/card02_extension.mp4",
]

results = {}
for path in FILES:
    name = os.path.basename(path)
    print(f"\n{'='*60}")
    print(f"上传: {name}")
    print(f"{'='*60}")
    try:
        group_id, asset_id = upload_asset(path)
        results[name] = {"group_id": group_id, "asset_id": asset_id}
        print(f"✓ {name} -> asset_id = {asset_id}")
    except Exception as e:
        results[name] = {"error": str(e)}
        print(f"✗ {name} 上传失败: {e}")

print("\n" + "=" * 60)
print("上传结果汇总")
print("=" * 60)
for name, info in results.items():
    if "asset_id" in info:
        print(f"  {name}: {info['asset_id']}")
    else:
        print(f"  {name}: ERROR - {info['error']}")
