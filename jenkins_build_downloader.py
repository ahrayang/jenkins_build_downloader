import os
import sys
import time
import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv
import tkinter as tk
from tkinter import simpledialog

# 실행 경로 처리 (스크립트 vs 실행파일)
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
CONF_PATH   = BASE_DIR / "config.json"
STATE_PATH  = BASE_DIR / "state.json"

load_dotenv(dotenv_path=str(BASE_DIR / ".env"))
JENKINS_URL  = os.getenv("JENKINS_URL")
JENKINS_USER = os.getenv("JENKINS_USER")
JENKINS_TOKEN= os.getenv("JENKINS_TOKEN")
if not all([JENKINS_URL, JENKINS_USER, JENKINS_TOKEN]):
    print("ERROR: .env에 JENKINS_URL, JENKINS_USER, JENKINS_TOKEN를 설정해주세요.")
    sys.exit(1)
auth = (JENKINS_USER, JENKINS_TOKEN)

with CONF_PATH.open(encoding="utf-8") as f:
    config = json.load(f)
platforms   = config.get("platforms", [])
base_folder = config.get("base_folder")
users       = config.get("users", [])

if not base_folder:
    root = tk.Tk(); root.withdraw()
    bf = simpledialog.askstring("최상위 폴더", "빌드를 저장할 최상위 폴더명을 입력하세요:")
    config["base_folder"] = bf.strip()
    with CONF_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    root.destroy()
    base_folder = config["base_folder"]

if not users or not users[0].get("platform_dirs") or len(users[0]["platform_dirs"]) != len(platforms):
    root = tk.Tk(); root.withdraw()
    dirs = {}
    for p in platforms:
        name = simpledialog.askstring("플랫폼 폴더", f"username을 포함한 {p} 저장용 폴더명을 입력하세요:(ex:ahra_{p})")
        dirs[p] = name.strip()
    config["users"] = [{"platform_dirs": dirs}]
    with CONF_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    root.destroy()
platform_dirs = config["users"][0]["platform_dirs"]

home_dir = Path.home()
base_dir = home_dir / base_folder
base_dir.mkdir(parents=True, exist_ok=True)

if STATE_PATH.exists():
    with STATE_PATH.open(encoding="utf-8") as f:
        state = json.load(f)
else:
    state = {}

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "downloader.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

KW_MAP = {
    "Client_Android": "binary.androidaab.",
    "Client_IOS":     "sol.ios.",
    "Client_Windows": "binary.win."
}

def timestamp():
    return datetime.now().strftime("%Y.%m.%d.%H.%M")

def jenkins_api(path: str):
    r = requests.get(urljoin(JENKINS_URL, path), auth=auth, timeout=10)
    r.raise_for_status()
    return r.json()

def list_subjobs(platform: str):
    data = jenkins_api(f"/job/{platform}/api/json?depth=1")
    jobs = [job["name"] for job in data.get("jobs", [])]
    logger.debug(f"{platform}의 서브잡 목록: {jobs}")
    return jobs

def fetch_and_download(platform: str, job: str):
    key = f"{platform}/{job}"
    try:
        info = jenkins_api(f"/job/{platform}/job/{job}/lastSuccessfulBuild/api/json")
    except requests.HTTPError as e:
        logger.error(f"{key} 조회 실패: {e}")
        return

    build_no = info.get("number")
    logger.debug(f"{key} 빌드 번호: {build_no}")
    if state.get(key) == build_no:
        logger.debug(f"{key} 최신 빌드 #{build_no}는 이미 다운로드됐습니다. 건너뜁니다.")
        return

    artifacts = info.get("artifacts", [])
    logger.debug(f"{key} artifacts: {[a['relativePath'] for a in artifacts]}")
    kw = KW_MAP.get(platform, "")
    targets = [a for a in artifacts if kw in a.get("relativePath", "").lower()]
    logger.debug(f"{key} 대상 artifacts: {targets}")
    if not targets:
        logger.warning(f"{key}에서 '{kw}' 포함된 artifact가 없습니다.")
        return

    job_folder = job.lower()
    save_path = base_dir / platform_dirs[platform] / job_folder
    save_path.mkdir(parents=True, exist_ok=True)

    for art in targets:
        rel          = art["relativePath"]
        artifact_url = urljoin(info["url"], "artifact/" + rel)
        fname        = f"{timestamp()}_{job}_{Path(rel).name}"
        temp_dest    = save_path / (fname + ".part")
        dest         = save_path / fname

        if dest.exists():
            logger.info(f"이미 존재: {dest} - 건너뜁니다.")
            continue

        logger.info(f"다운로드 시작: {artifact_url} → {temp_dest}")
        with requests.get(artifact_url, auth=auth, stream=True, timeout=20) as r:
            r.raise_for_status()
            with temp_dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        temp_dest.replace(dest)
        logger.info(f"완료: {dest}")

    state[key] = build_no
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def main():
    executor = ThreadPoolExecutor(max_workers=8)
    try:
        while True:
            futures = [executor.submit(fetch_and_download, p, j) for p in platforms for j in list_subjobs(p)]
            for future in as_completed(futures):
                future.result()
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("프로그램 종료")
    finally:
        executor.shutdown(wait=False)

if __name__ == "__main__":
    main()