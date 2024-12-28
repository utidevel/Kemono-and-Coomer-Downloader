import locale
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable

import i18n
import requests
import yaml
from peewee import SqliteDatabase, Model, CharField
from tqdm import tqdm


@dataclass
class Config:
    proxy_username: str = field(default=None)
    proxy_password: str = field(default=None)
    proxy_url: str = field(default=None)
    save_info: bool = field(default=False)
    post_info: str = field(default=None)
    process_from_oldest: bool = field(default=False)
    get_empty_posts: bool = field(default=False)
    debug_verify_ssl: bool = field(default=True)

    @classmethod
    def from_yaml(cls, file_path: str) -> "Config":
        """
        Load configuration from a YAML file and return a Config instance.
        """
        try:
            with open(file_path, 'r') as file:
                data = yaml.safe_load(file) or {}
            return cls(**data)
        except FileNotFoundError:
            print(i18n.t("file_not_found", file_path=file_path))
            raise Exception(i18n.t("config_not_found"))
        except yaml.YAMLError as e:
            raise Exception(i18n.t("config_errors")) from e

    def save_to_yaml(self, file_path: str):
        """
        Save the current configuration to a YAML file.
        """
        try:
            with open(file_path, 'w') as file:
                yaml.dump(vars(self), file, default_flow_style=False)
            print(i18n.t("config_saved", file_path=file_path))
        except Exception as e:
            print(f"{i18n.t('error_saving_config', file_path=file_path)}: {e}")

    def __str__(self):
        masked_password = '*' * len(self.proxy_password) if self.proxy_password else None
        return (f"Config("
                f"proxy_username='{self.proxy_username}', "
                f"proxy_password='{masked_password}', "
                f"proxy_url='{self.proxy_url}', "
                f"save_info={self.save_info}, "
                f"post_info={self.post_info}, "
                f"process_from_oldest={self.process_from_oldest}, "
                f"get_empty_posts={self.get_empty_posts}, "
                f"debug_verify_ssl={self.debug_verify_ssl})")

    def get_requests_proxy(self):
        """
        Get a dictionary of proxies for requests.
        """
        if self.proxy_url and self.proxy_url.strip():
            if self.proxy_username and self.proxy_password:
                auth = f"{self.proxy_username}:{self.proxy_password}@"
            else:
                auth = ""
            return {
                'http': f"http://{auth}{self.proxy_url}",
                'https': f"http://{auth}{self.proxy_url}"
            }
        return None


class Posts:
    def __init__(self, config: Config):
        self.config: Config = config

    @staticmethod
    def get_base_config(profile_url: str):
        """
        Dynamically configure base URLs and directories based on the profile URL domain.
        """
        domain = profile_url.split('/')[2]

        if domain not in ['kemono.su', 'coomer.su']:
            raise ValueError(f"Unsupported domain: {domain}")

        base_api_url = f"https://{domain}/api/v1"
        base_server = f"https://{domain}"
        base_dir = domain.split('.')[0]  # Extract 'kemono' or 'coomer'

        return base_api_url, base_server, base_dir

    @staticmethod
    def is_offset(value: str) -> bool:
        """
        Determine if the value is an offset (up to 5 digits) or an ID.
        """
        try:
            return len(value) <= 5 and value.isdigit()
        except ValueError:
            return False

    def parse_fetch_mode(self, fetch_mode: str, total_count: int) -> List:
        """
        Parse the fetch mode and return corresponding offsets.
        """
        if fetch_mode == "all":
            return list(range(0, total_count, 50))

        if fetch_mode.isdigit():
            return [int(fetch_mode)] if self.is_offset(fetch_mode) else ["id:" + fetch_mode]

        if "-" in fetch_mode:
            start, end = fetch_mode.split("-")
            start = 0 if start == "start" else int(start)
            end = total_count if end == "end" else int(end)

            if start <= total_count and end <= total_count:
                return [start + i * 50 for i in range(math.ceil((end - start) / 50))]
            return [f"id:{start}-{end}"]

        raise ValueError(f"{i18n.t('invalid_search_mode')}: {fetch_mode}")

    @staticmethod
    def get_artist_info(profile_url: str):
        """
        Extract service and user ID from the profile URL.
        """
        parts = profile_url.strip('/').split("/")
        return parts[-3], parts[-1]

    def fetch_posts(self, base_api_url: str, service: str, user_id: str, offset: int = 0):
        """
        Fetch posts from the API.
        """
        url = f"{base_api_url}/{service}/user/{user_id}/posts-legacy?o={offset}"
        response = requests.get(url, proxies=self.config.get_requests_proxy(), verify=self.config.debug_verify_ssl)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def process_posts(posts: List[Dict], previews: List[Dict], attachments_data: List[Dict],
                      page_number: int, offset: int, base_server: str,
                      save_empty_files: bool = True, id_filter: Optional[Callable[[str], bool]] = None) -> List[Dict]:
        """
        Process posts and organize file links.
        """
        processed = []
        for post in posts:
            if id_filter and not id_filter(post['id']):
                continue

            result = {
                "id": post["id"],
                "user": post["user"],
                "service": post["service"],
                "title": post["title"],
                "link": f"{base_server}/{post['service']}/user/{post['user']}/post/{post['id']}",
                "page": page_number,
                "offset": offset,
                "files": []
            }

            all_data = previews + attachments_data
            if "file" in post and post["file"]:
                file_data = next((item for item in all_data if item["path"] == post["file"]["path"]), None)
                if file_data:
                    file_url = f"{file_data['server']}/data{post['file']['path']}"
                    result["files"].append({"name": post["file"]["name"], "url": file_url})

            for attachment in post.get("attachments", []):
                attachment_data = next((item for item in all_data if item["path"] == attachment["path"]), None)
                if attachment_data:
                    file_url = f"{attachment_data['server']}/data{attachment['path']}"
                    result["files"].append({"name": attachment["name"], "url": file_url})

            if not save_empty_files and not result["files"]:
                continue

            processed.append(result)

        return processed

    @staticmethod
    def sanitize_filename(value: str) -> str:
        """
        Remove characters that could break directory or file creation.
        """
        return value.replace("/", "_").replace("\\", "_")

    def run(self, profile_url: str, fetch_mode: str):
        """
        Run the post extraction process based on the profile URL and fetch mode.
        """
        base_api_url, base_server, base_dir = self.get_base_config(profile_url)
        service, user_id = self.get_artist_info(profile_url)

        initial_data = self.fetch_posts(base_api_url, service, user_id)
        name = initial_data["props"]["name"]
        count = initial_data["props"]["count"]

        safe_name = self.sanitize_filename(name)
        safe_service = self.sanitize_filename(service)
        safe_user_id = self.sanitize_filename(user_id)

        try:
            offsets = self.parse_fetch_mode(fetch_mode, count)
        except ValueError as e:
            print(e)
            return

        id_filter = None
        if isinstance(offsets[0], str) and offsets[0].startswith("id:"):
            id_range = offsets[0].split(":")[1]
            if "-" in id_range:
                id1, id2 = map(int, id_range.split("-"))
                id_filter = lambda x: id1 <= int(x) <= id2
            else:
                id_filter = lambda x: x == int(id_range)
            offsets = list(range(0, count, 50))

        processed_posts = []
        for offset in offsets:
            page_number = (offset // 50) + 1
            post_data = self.fetch_posts(base_api_url, service, user_id, offset)
            processed_posts += self.process_posts(
                posts=post_data["results"],
                previews=[item for sublist in post_data.get("result_previews", []) for item in sublist],
                attachments_data=[item for sublist in post_data.get("result_attachments", []) for item in sublist],
                page_number=page_number,
                offset=offset,
                base_server=base_server,
                save_empty_files=self.config.get_empty_posts,
                id_filter=id_filter
            )

        return base_dir, safe_service, safe_user_id, safe_name, processed_posts


class Down:
    def __init__(self, config: Config):
        """
        Initialize the downloader with a SQLite database and configuration.
        """
        self.db = SqliteDatabase('downloaded.db')
        self.model = self.create_model()
        self.db.connect()
        self.db.create_tables([self.model])  # Ensure the table exists
        self.config: Config = config

    def create_model(self):
        """
        Define the SQLite model for tracking downloaded posts.
        """
        class DownloadedPosts(Model):
            value = CharField(unique=True)  # Unique identifier for downloaded posts

            class Meta:
                database = self.db  # Link the model to the SQLite database

        return DownloadedPosts

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """
        Sanitize a filename by removing invalid characters and replacing spaces with underscores.
        """
        filename = re.sub(r'[\\/*?\"<>|]', '', filename)  # Remove invalid characters
        return filename.replace(' ', '_')

    def download_file(self, file_url: str, save_path: str):
        """
        Download a file from a URL and save it to the specified path.
        Includes retry logic and a progress bar.
        """
        max_retries = 5
        retry_delay = 5  # Delay in seconds between retries
        attempt = 0

        while attempt < max_retries:
            try:
                print(i18n.t("download_attempt", text1=attempt + 1, text2=file_url), flush=True)
                response = requests.get(file_url, stream=True, proxies=self.config.get_requests_proxy(),
                                        verify=self.config.debug_verify_ssl)
                response.raise_for_status()

                # Get the total file size from headers
                total_size = int(response.headers.get('content-length', 0))

                # Save the file with a progress bar
                with open(save_path, 'wb') as f:
                    with tqdm(
                        total=total_size, unit='B', unit_scale=True, unit_divisor=1024,
                        desc="Downloading", leave=False
                    ) as pbar:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                pbar.update(len(chunk))

                downloaded_size = os.path.getsize(save_path)
                if downloaded_size == total_size:
                    print(i18n.t("download_success", file_url=file_url, downloaded_size=downloaded_size))
                else:
                    print(i18n.t("download_incomplete", file_url=file_url, total_size=total_size, downloaded_size=downloaded_size))
                    raise Exception(i18n.t("could_not_complete"))

                print(i18n.t("download_complete", file_url=file_url), flush=True)
                return  # Exit if the download is successful

            except Exception as e:
                attempt += 1
                print(f"Warning: Attempt {attempt} failed for {file_url}: {e}")
                if attempt < max_retries:
                    print(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    print(f"Download failed after {max_retries} attempts.")
                    return

    def process_post(self, post: dict, base_folder: str):
        """
        Process a single post by downloading all its associated files.
        """
        post_id = post.get("id")
        post_folder = os.path.join(base_folder, post_id)
        os.makedirs(post_folder, exist_ok=True)

        print(f"Processing post ID {post_id}")

        # Download files in parallel
        with ThreadPoolExecutor(max_workers=3) as executor:
            for file_url, file_save_path in self.generate_downloads(post, post_folder):
                executor.submit(self.download_file, file_url, file_save_path)

        print(f"Post {post_id} downloaded successfully.")

    def generate_downloads(self, post: dict, post_folder: str):
        """
        Generate a list of files to download for a post.
        """
        for file_index, file in enumerate(post.get("files", []), start=1):
            original_name = file.get("name")
            file_url = file.get("url")
            sanitized_name = self.sanitize_filename(original_name)
            new_filename = f"{file_index}-{sanitized_name}"
            file_save_path = os.path.join(post_folder, new_filename)
            yield file_url, file_save_path

    def run(self, base_folder: str, post_json: dict):
        """
        Run the download process for a single post.
        """
        # Set up the base folder for saving posts
        base_folder = os.path.join(base_folder, "posts")
        os.makedirs(base_folder, exist_ok=True)

        # Check if the post has already been downloaded
        try:
            self.model.get(self.model.value == post_json.get("id"))
            print(i18n.t("already_downloaded"))
            return
        except self.model.DoesNotExist:
            print(i18n.t("starting_download"))

        # Process and download the post
        self.process_post(post_json, base_folder)

        # Mark the post as downloaded
        self.model.create(value=post_json.get("id"))
        time.sleep(2)  # Pause between posts to avoid overloading the server


class Downloader:
    def __init__(self):
        """
        Initialize the downloader with system locale and configurations.
        """
        system_locale, _ = locale.getdefaultlocale()
        language_code = system_locale.split("_")[0]
        i18n.set("locale", language_code)
        i18n.set('filename_format', '{locale}.{format}')
        i18n.set('file_format', 'yaml')
        i18n.set('skip_locale_root_data', True)
        i18n.load_path.append("locales")
        i18n.set("locale", "en")

        self.config = Config.from_yaml("conf.yaml")
        self.down = Down(self.config)
        self.posts = Posts(self.config)

    @staticmethod
    def display_logo():
        """
        Display the application logo.
        """
        print(i18n.t("logo"))

    def run_download_script(self, base_dir, service, user_id, username, json_posts):
        """
        Run the download script with the given JSON posts and provide detailed tracking.
        """
        try:
            total_posts = len(json_posts)
            total_files = sum(len(post['files']) for post in json_posts)

            print(i18n.t("post_extract_complete", total_posts=total_posts))
            print(i18n.t("number_of_files", total_files=total_files))
            print(i18n.t("starting_downloads"))

            # Sort posts based on configuration
            json_posts = sorted(json_posts, key=lambda x: x['id'], reverse=self.config.process_from_oldest)

            # Normalize base folder for posts
            posts_folder = str(os.path.join(base_dir, service, f"{username} - {user_id}"))

            for post in json_posts:
                post_folder = os.path.join(posts_folder, "posts", post['id'])
                expected_files_count = len(post['files'])

                try:
                    self.down.run(posts_folder, post)
                    current_files_count = len([
                        f for f in os.listdir(post_folder)
                        if os.path.isfile(os.path.join(post_folder, f))
                    ])

                    if current_files_count == expected_files_count:
                        print(i18n.t("post_downloaded", post_id=post['id'], current_files_count=current_files_count, expected_files_count=expected_files_count))
                    else:
                        print(i18n.t("post_partially_downloaded", post_id=post['id'], current_files_count=current_files_count, expected_files_count=expected_files_count))

                except Exception as e:
                    print(f"{i18n.t('post_download_error', post_id=post['id'])}: {e}")
                    time.sleep(0.5)

            print(i18n.t("post_all_complete"))

        except Exception as e:
            print(f"Unexpected error: {e}")
            raise

    def download_profile_posts(self):
        """
        Download posts from a profile.
        """
        self.display_logo()
        print(i18n.t("download_profile_posts"))

        choice = input(i18n.t("download_profile_posts_choice"))



        profile_link = input("Paste the profile link: ")
        try:
            json_posts = None
            if choice == '1':
                base_dir, service, user_id, name, json_posts = self.posts.run(profile_link, 'all')
            elif choice == '2':
                page = input("Enter the page number (0 = first page, 50 = second, etc.): ")
                base_dir, service, user_id, name, json_posts = self.posts.run(profile_link, page)
            elif choice == '3':
                start_page = input("Enter the start page (start, 0, 50, 100, etc.): ")
                end_page = input("Enter the final page (or use end, 300, 350, 400): ")
                base_dir, service, user_id, name, json_posts = self.posts.run(profile_link, f"{start_page}-{end_page}")
            elif choice == '4':
                first_post = input("Paste the link or ID of the first post: ")
                second_post = input("Paste the link or ID from the second post: ")
                first_id = first_post.split('/')[-1] if '/' in first_post else first_post
                second_id = second_post.split('/')[-1] if '/' in second_post else second_post
                base_dir, service, user_id, name, json_posts = self.posts.run(profile_link, f"{first_id}-{second_id}")
            else:
                return

            if json_posts:
                self.run_download_script(base_dir, service, user_id, name, json_posts)
            else:
                print("The JSON path could not be found.")

        except Exception as e:
            print(f"Error processing profile: {e}")

        input("\nPress Enter to continue...")

    def customize_settings(self):
        """
        Customize application settings via an interactive menu.
        """
        config = Config.from_yaml("conf.yaml")

        while True:
            self.display_logo()
            print(i18n.t("customize_settings", get_empty_posts=self.config.get_empty_posts, process_from_oldest=self.config.process_from_oldest, save_info=self.config.save_info, post_info=self.config.post_info))
            choice = input(i18n.t("download_profile_posts_choice"))

            if choice == '1':
                config.get_empty_posts = not config.get_empty_posts
            elif choice == '2':
                config.process_from_oldest = not config.process_from_oldest
            elif choice == '3':
                config.save_info = not config.save_info
            elif choice == '4':
                config.post_info = 'txt' if config.post_info == 'md' else 'md'
            elif choice == '5':
                break
            else:
                print(i18n.t("invalid_option_try_again"))

            config.save_to_yaml("conf.yaml")
            print("\nUpdated configurations.")
            time.sleep(1)

    def main_menu(self):
        """
        Main menu of the application.
        """
        while True:
            self.display_logo()
            print(i18n.t("choose_an_option"))

            choice = input(i18n.t("choose_an_option_choice"))
            if choice == '1':
                self.download_profile_posts()
            elif choice == '2':
                self.customize_settings()
            elif choice == '3':
                os.remove("downloaded.db")
            elif choice == '4':
                print(i18n.t("leave_program"))
                break
            else:
                print(i18n.t("invalid_option"))


if __name__ == "__main__":
    Downloader().main_menu()
