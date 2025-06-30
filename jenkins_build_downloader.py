#!/usr/bin/env python3
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

BASE_DIR = Path(__file__).parent
CONF_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

load_dotenv(BASE_DIR / ".env")
JENKINS_URL = os.getenv("JENKINS_URL")
JENKINS_USER = os.getenv("JENKINS_USER")
JENKINS_TOKEN = os.getenv("JENKINS_TOKEN")
if not all([JENKINS_URL, JENKINS_USER, JENKINS_TOKEN]):
    print("ERROR: .env에 JENKINS_URL, JENKINS_USER, JENKINS_TOKEN를 설정해주세요.")
    sys.exit(1)

auth = (JENKINS_USER, JENKINS_TOKEN)

config = json.loads(CONF_PATH.read_text(encoding="utf-8"))
user_conf = config["users"][0]
platforms = config["platforms"]

if not user_conf.get("platform_dirs") or len(user_conf.get("platform_dirs", {})) != len(platforms):
    root = tk.Tk()
    root.withdraw()
    dirs = {}
    for p in platforms:
        name = simpledialog.askstring("폴더 이름", f"{p} 다운로드용 로컬 폴더명을 입력하세요:")
        dirs[p] = name.strip()
    user_conf["platform_dirs"] = dirs
    CONF_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    root.destroy()

base_dir = Path(user_conf["base_dir"]).expanduser().resolve()
platform_dirs = user_conf["platform_dirs"]

state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "downloader.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger()

def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def timestamp():
    return datetime.now().strftime("%Y.%m.%d.%H.%M")

def jenkins_api(endpoint: str):
    url = urljoin(JENKINS_URL, endpoint)
    res = requests.get(url, auth=auth, timeout=10)
    res.raise_for_status()
    return res.json()

def list_subjobs(platform: str):
    data = jenkins_api(f"/job/{platform}/api/json?depth=1")
    return [job["name"] for job in data.get("jobs", [])]

def fetch_and_download(platform: str, job: str):
    key = f"{platform}/{job}"
    try:
        info = jenkins_api(f"/job/{platform}/job/{job}/lastSuccessfulBuild/api/json")
    except requests.HTTPError as e:
        logger.error(f"{key} 정보 조회 실패: {e}")
        return

    build_no = info.get("number")
    if state.get(key) == build_no:
        return

    artifacts = info.get("artifacts", [])
    if not artifacts:
        logger.warning(f"{key}에 artifact가 없습니다.")
        return

    filter_kw = {
        "Client_Windows": "Binary.Win.",
        "Client_Android": "Binary.AndroidApk.",
        "Client_IOS": "Sol.iOS."
    }.get(platform, "")

    targets = [a for a in artifacts if filter_kw in a.get("relativePath", "")]
    if not targets:
        logger.warning(f"{key}에서 '{filter_kw}' 포함된 artifact를 찾을 수 없습니다.")
        return

    user_folder = base_dir / platform_dirs[platform]
    for art in targets:
        rel_path = art["relativePath"]
        download_url = urljoin(info["url"], "artifact/" + rel_path)

        ensure_dir(user_folder)
        filename = f"{timestamp()}_{job}_{Path(rel_path).name}"
        dest = user_folder / filename

        if dest.exists():
            logger.info(f"이미 존재: {dest} → 다운로드 건너뜀")
            continue

        logger.info(f"다운로드 시작: {key} #{build_no} → {dest}")
        with requests.get(download_url, auth=auth, stream=True, timeout=20) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
        logger.info(f"다운로드 완료: {dest}")

    state[key] = build_no


def main():
    executor = ThreadPoolExecutor(max_workers=8)
    try:
        while True:
            futures = []
            for plt in platforms:
                for jb in list_subjobs(plt):
                    futures.append(executor.submit(fetch_and_download, plt, jb))
            for future in as_completed(futures):
                _ = future.result()

            STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("프로그램 종료")
    finally:
        executor.shutdown(wait=False)

if __name__ == "__main__":
    main()