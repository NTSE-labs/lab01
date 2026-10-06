import hashlib
import os
import html
import io
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, UnidentifiedImageError

BASE_URL = "https://yandex.ru/images/search"
PROJECT_DIR = Path(__file__).resolve().parent
DATASET_DIR = PROJECT_DIR / "dataset"
MANIFEST_PATH = PROJECT_DIR / "manifest.jsonl"

TARGET_IMAGES = 1200
MAX_PAGE = 80
MIN_SIDE = 600
REQUEST_TIMEOUT = 25
REQUEST_DELAY = 0.35
DOWNLOAD_RETRIES = 3
BROWSER_WAIT = 12
SCROLLS_PER_PAGE = 6
SCROLL_DELAY = 0.8
MAX_EMPTY_PAGES = 3

SEARCH_QUERIES = {
    "cat": [
        "cat",
        "кот",
        "кошка",
        "домашняя кошка",
    ],
    "dog": [
        "dog",
        "собака",
        "пёс",
    ],
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def normalise_url(url: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    return urljoin("https://yandex.ru", url)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_seen() -> tuple[set[str], set[str]]:
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()

    if MANIFEST_PATH.exists():
        with MANIFEST_PATH.open("r", encoding="utf-8") as manifest:
            for line in manifest:
                if not line.strip():
                    continue
                record = json.loads(line)
                seen_urls.add(record["source_url"])
                seen_hashes.add(record["sha256"])

    for image_path in DATASET_DIR.glob("*/*.jpg"):
        try:
            seen_hashes.add(sha256_bytes(image_path.read_bytes()))
        except OSError:
            continue

    return seen_urls, seen_hashes


def save_manifest(record: dict[str, Any]) -> None:
    with MANIFEST_PATH.open("a", encoding="utf-8") as manifest:
        manifest.write(json.dumps(record, ensure_ascii=False) + "\n")


def get_existing_count(class_dir: Path) -> int:
    return len(list(class_dir.glob("*.jpg")))


def next_filename(class_dir: Path) -> Path:
    existing_indices = set()
    for path in class_dir.glob("*.jpg"):
        try:
            existing_indices.add(int(path.stem))
        except ValueError:
            continue

    for index in range(TARGET_IMAGES):
        if index not in existing_indices:
            return class_dir / (str(index).zfill(4) + ".jpg")

    raise RuntimeError(f"В папке {class_dir} не осталось свободных имён файлов")


def extract_image_urls(response_text: str) -> list[str]:
    soup = BeautifulSoup(response_text, "html.parser")
    image_urls: list[str] = []

    images_app = soup.find(
        attrs={"id": lambda value: value and value.startswith("ImagesApp-")}
    )
    if images_app and images_app.get("data-state"):
        raw_state = html.unescape(images_app["data-state"])
        try:
            state = json.loads(raw_state)
            entities = (
                state.get("initialState", {})
                .get("serpList", {})
                .get("items", {})
                .get("entities", {})
            )
            if isinstance(entities, dict):
                for item in entities.values():
                    if not isinstance(item, dict):
                        continue
                    candidates = [
                        item.get("origUrl"),
                        item.get("image"),
                        (item.get("viewerData") or {}).get("img_href"),
                    ]
                    for candidate in candidates:
                        if not candidate:
                            continue
                        url = str(candidate)
                        if url.startswith("//"):
                            url = f"https:{url}"
                        elif url.startswith("/"):
                            url = urljoin("https://yandex.ru", url)
                        if url.startswith("http"):
                            image_urls.append(url)
                            break
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    for item in soup.select(".serp-item[data-bem]"):
        data_bem = item.get("data-bem")
        if not data_bem:
            continue
        try:
            payload = json.loads(html.unescape(data_bem))
            image_url = payload.get("serp-item", {}).get("img_href")
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        if image_url:
            image_urls.append(normalise_url(image_url))

    return list(dict.fromkeys(image_urls))


def create_driver() -> webdriver.Chrome:
    options = Options()
    if os.getenv("HEADLESS", "1") != "0":
        options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=options)


def search_images(
    driver: webdriver.Chrome,
    query: str,
    page: int,
) -> list[str]:
    url = (
        f"{BASE_URL}?text={quote(query)}&p={page}"
        "&nomisspell=1&noreask=1&isize=large&family=yes"
    )
    driver.get(url)

    try:
        WebDriverWait(driver, BROWSER_WAIT).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, '[id^="ImagesApp-"]')
            )
        )
    except TimeoutException:
        pass

    for _ in range(SCROLLS_PER_PAGE):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(SCROLL_DELAY)

    source = driver.page_source
    image_urls = extract_image_urls(source)

    if not image_urls:
        page_text = driver.page_source.lower()
        if "showcaptcha" in driver.current_url.lower() or "captcha" in page_text:
            raise RuntimeError(
                "Яндекс запросил CAPTCHA. Запустите скрипт с HEADLESS=0, "
                "пройдите проверку в Chrome и перезапустите скрипт."
            )

    return image_urls


def download_bytes(
    session: requests.Session,
    image_url: str,
) -> bytes | None:
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            download_headers = dict(HEADERS)
            download_headers["Referer"] = "https://yandex.ru/images/"
            response = session.get(
                image_url,
                headers=download_headers,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            response.raise_for_status()

            if not response.content:
                raise ValueError("Пустой response.content")

            return response.content
        except (requests.RequestException, ValueError) as exc:
            if attempt == DOWNLOAD_RETRIES:
                print(f"Не удалось скачать изображение: {exc}")
                return None
            time.sleep(attempt)

    return None


def convert_to_jpeg(image_bytes: bytes) -> tuple[bytes, tuple[int, int]] | None:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.verify()

        with Image.open(io.BytesIO(image_bytes)) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            if min(width, height) < MIN_SIDE:
                return None

            if image.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", image.size, "white")
                if image.mode == "P":
                    image = image.convert("RGBA")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")

            buffer = io.BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=95,
                optimize=True,
            )
            return buffer.getvalue(), (width, height)
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def download_class(
    session: requests.Session,
    driver: webdriver.Chrome,
    class_name: str,
    queries: list[str],
    seen_urls: set[str],
    seen_hashes: set[str],
) -> None:
    class_dir = DATASET_DIR / class_name
    class_dir.mkdir(parents=True, exist_ok=True)

    count = get_existing_count(class_dir)
    if count >= TARGET_IMAGES:
        print(f"{class_name}: уже собрано {count} изображений")
        return

    print(f"\nСбор изображений для класса {class_name}: {count}/{TARGET_IMAGES}")

    for query in queries:
        empty_pages_count = 0
        for page in range(MAX_PAGE):
            if count >= TARGET_IMAGES:
                return

            try:
                image_urls = search_images(driver, query, page)
            except RuntimeError:
                raise
            except (requests.RequestException, WebDriverException) as exc:
                print(f"[{class_name}] Ошибка поиска: запрос {query!r}, страница {page}: {exc}")
                time.sleep(2)
                continue

            if not image_urls:
                print(f"[{class_name}] Нет результатов: запрос {query!r}, страница {page}")
                empty_pages_count += 1
                if empty_pages_count >= MAX_EMPTY_PAGES:
                    print(f"[{class_name}] Слишком много пустых страниц ({empty_pages_count}), остановка")
                    break
                continue

            empty_pages_count = 0

            new_urls = [url for url in image_urls if url not in seen_urls]
            print(
                f"[{class_name}] запрос={query!r}, страница={page}, "
                f"количество кандитатов={len(new_urls)}"
            )

            for image_url in new_urls:
                if count >= TARGET_IMAGES:
                    break

                seen_urls.add(image_url)
                time.sleep(REQUEST_DELAY)
                raw_bytes = download_bytes(session, image_url)
                if raw_bytes is None:
                    continue

                raw_hash = sha256_bytes(raw_bytes)
                if raw_hash in seen_hashes:
                    continue

                converted = convert_to_jpeg(raw_bytes)
                if converted is None:
                    continue

                jpeg_bytes, dimensions = converted
                jpeg_hash = sha256_bytes(jpeg_bytes)
                if jpeg_hash in seen_hashes:
                    continue

                file_path = next_filename(class_dir)
                file_path.write_bytes(jpeg_bytes)

                record = {
                    "class": class_name,
                    "file": str(file_path.relative_to(PROJECT_DIR)),
                    "source_url": image_url,
                    "sha256": jpeg_hash,
                    "width": dimensions[0],
                    "height": dimensions[1],
                    "query": query,
                    "page": page,
                }
                save_manifest(record)
                seen_hashes.add(jpeg_hash)
                count += 1
                print(f"Сохранено {file_path.name} ({count}/{TARGET_IMAGES})")

                time.sleep(REQUEST_DELAY)

    if count < TARGET_IMAGES:
        raise RuntimeError(
            f"Недостаточно подходящих изображений для класса {class_name}: "
            f"{count}/{TARGET_IMAGES}. Повторите запуск скрипта позже."
        )


def main() -> None:
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    seen_urls, seen_hashes = load_seen()

    driver = None
    try:
        driver = create_driver()
        with requests.Session() as session:
            session.headers.update(HEADERS)
            for class_name, queries in SEARCH_QUERIES.items():
                download_class(
                    session,
                    driver,
                    class_name,
                    queries,
                    seen_urls,
                    seen_hashes,
                )
    finally:
        if driver is not None:
            driver.quit()

    for class_name in SEARCH_QUERIES:
        count = get_existing_count(DATASET_DIR / class_name)
        print(f"{class_name}: {count} изображений")


if __name__ == "__main__":
    main()
