"""Public-release check/staging only. Intentionally has no apply or restart action."""
import argparse
import json
import os
from pathlib import Path

import httpx

from gsm_api.releases import ReleaseError, check_release, stage_release


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 GitHub Release 或下载并校验源码发布包，不修改运行服务。")
    parser.add_argument("action", choices=["check", "stage"])
    parser.add_argument("--repository", default=os.environ.get("GSM_UPDATE_REPOSITORY", ""), help="公开 GitHub owner/repository")
    parser.add_argument("--channel", choices=["stable", "prerelease"], default=os.environ.get("GSM_UPDATE_CHANNEL", "stable"))
    parser.add_argument("--destination", type=Path, default=Path("./gsm-updates"))
    args = parser.parse_args()
    try:
        if args.action == "check":
            result = check_release(args.repository, args.channel)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] in {"available", "current", "no_release"} else 1
        folder = stage_release(args.repository, args.channel, args.destination)
        print(f"已下载并校验：{folder.resolve()}。尚未安装，请按部署指南备份和升级。")
        return 0
    except (ReleaseError, httpx.HTTPError, OSError, ValueError):
        print("更新暂存失败：请检查公开仓库、发布清单、网络、目录权限和可用空间。运行服务未被修改。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
