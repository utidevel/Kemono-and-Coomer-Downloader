import locale
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

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
    def get_base_config(profile_url):
        """
        Dynamically configure base URLs and directories based on the profile URL domain
        """
        # Extract domain from the profile URL
        domain = profile_url.split('/')[2]

        if domain not in ['kemono.su', 'coomer.su']:
            raise ValueError(f"Unsupported domain: {domain}")

        base_api_url = f"https://{domain}/api/v1"
        base_server = f"https://{domain}"
        base_dir = domain.split('.')[0]  # 'kemono' or 'coomer'

        return base_api_url, base_server, base_dir

    @staticmethod
    def is_offset(value):
        """Determina se o valor é um offset (até 5 dígitos) ou um ID."""
        try:
            # Tenta converter para inteiro e verifica o comprimento
            return isinstance(int(value), int) and len(value) <= 5
        except ValueError:
            # Se não for um número, não é offset
            return False

    def parse_fetch_mode(self, fetch_mode, total_count):
        """
        Analisa o modo de busca e retorna os offsets correspondentes
        """
        # Caso especial: buscar todos os posts
        if fetch_mode == "all":
            return list(range(0, total_count, 50))

        # Se for um número único (página específica)
        if fetch_mode.isdigit():
            if self.is_offset(fetch_mode):
                return [int(fetch_mode)]
            else:
                # Se for um ID específico, retorna como tal
                return ["id:" + fetch_mode]

        # Caso seja um intervalo
        if "-" in fetch_mode:
            start, end = fetch_mode.split("-")

            # Tratar "start" e "end" especificamente
            if start == "start":
                start = 0
            else:
                start = int(start)

            if end == "end":
                end = total_count
            else:
                end = int(end)

            # Se os valores são offsets
            if start <= total_count and end <= total_count:
                # Calcular o número de páginas necessárias para cobrir o intervalo
                # Usa ceil para garantir que inclua a página final

                num_pages = math.ceil((end - start) / 50)

                # Gerar lista de offsets
                return [start + i * 50 for i in range(num_pages)]

            # Se parecem ser IDs, retorna o intervalo de IDs
            return ["id:" + str(start) + "-" + str(end)]

        raise ValueError(f"{i18n.t("invalid_search_mode")}: {fetch_mode}")

    @staticmethod
    def get_artist_info(profile_url):
        # Extrair serviço e user_id do URL
        parts = profile_url.split("/")
        service = parts[-3]
        user_id = parts[-1]
        return service, user_id

    def fetch_posts(self, base_api_url, service, user_id, offset=0):
        # Buscar posts da API
        url = f"{base_api_url}/{service}/user/{user_id}/posts-legacy?o={offset}"
        response = requests.get(url, proxies=self.config.get_requests_proxy(), verify=self.config.debug_verify_ssl)
        response.raise_for_status()
        return response.json()

    # @staticmethod
    # def save_json_incrementally(file_path, new_posts, start_offset, end_offset):
    #     # Criar um novo dicionário com os posts atuais
    #     data = {
    #         "total_posts": len(new_posts),
    #         "posts": new_posts
    #     }
    #
    #     # Salvar o novo arquivo, substituindo o existente
    #     with open(file_path, "w", encoding="utf-8") as f:
    #         json.dump(data, f, indent=4, ensure_ascii=False)

    @staticmethod
    def process_posts(posts, previews, attachments_data, page_number, offset, base_server, save_empty_files=True, id_filter=None):
        # Processar posts e organizar os links dos arquivos
        processed = []
        for post in posts:
            # Filtro de ID se especificado
            if id_filter and not id_filter(post['id']):
                continue

            result = {"id": post["id"], "user": post["user"], "service": post["service"], "title": post["title"], "link": f"{base_server}/{post['service']}/user/{post['user']}/post/{post['id']}", "page": page_number, "offset": offset, "files": []}

            # Combina previews e attachments_data em uma única lista para busca
            all_data = previews + attachments_data

            # Processar arquivos no campo file
            if "file" in post and post["file"]:
                matching_data = next((item for item in all_data if item["path"] == post["file"]["path"]), None)
                if matching_data:
                    file_url = f"{matching_data['server']}/data{post['file']['path']}"
                    if file_url not in [f["url"] for f in result["files"]]:
                        result["files"].append({"name": post["file"]["name"], "url": file_url})

            # Processar arquivos no campo attachments
            for attachment in post.get("attachments", []):
                matching_data = next((item for item in all_data if item["path"] == attachment["path"]), None)
                if matching_data:
                    file_url = f"{matching_data['server']}/data{attachment['path']}"
                    if file_url not in [f["url"] for f in result["files"]]:
                        result["files"].append({"name": attachment["name"], "url": file_url})

            # Ignorar posts sem arquivos se save_empty_files for False
            if not save_empty_files and not result["files"]:
                continue

            processed.append(result)

        return processed

    @staticmethod
    def sanitize_filename(value):
        """Remove caracteres que podem quebrar a criação de pastas."""
        return value.replace("/", "_").replace("\\", "_")

    def run(self, profile_url: str, fetch_mode: str):

        # Configure base URLs dynamically
        base_api_url, base_server, base_dir = self.get_base_config(profile_url)

        # Fetch the first set of posts for general information
        service, user_id = self.get_artist_info(profile_url)
        initial_data = self.fetch_posts(base_api_url, service, user_id, offset=0)
        name = initial_data["props"]["name"]
        count = initial_data["props"]["count"]

        # Sanitizar os valores
        safe_name = self.sanitize_filename(name)
        safe_service = self.sanitize_filename(service)
        safe_user_id = self.sanitize_filename(user_id)

        try:
            offsets = self.parse_fetch_mode(fetch_mode, count)
        except ValueError as e:
            print(e)
            return

        # Verificar se é busca por ID específico
        id_filter = None
        found_ids = set()
        if isinstance(offsets[0], str) and offsets[0].startswith("id:"):
            # Extrair IDs para filtro
            id_range = offsets[0].split(":")[1]

            if "-" in id_range:
                id1, id2 = map(str, sorted(map(int, id_range.split("-"))))
                id_filter = lambda x: id1 <= str(x) <= id2
            else:
                id_filter = lambda x: x == id_range
            # Redefinir offsets para varrer todas as páginas
            offsets = list(range(0, count, 50))

        processed_posts = []

        for offset in offsets:
            page_number = (offset // 50) + 1
            post_data = self.fetch_posts(base_api_url, service, user_id, offset=offset)
            posts = post_data["results"]
            previews = [item for sublist in post_data.get("result_previews", []) for item in sublist]
            attachments = [item for sublist in post_data.get("result_attachments", []) for item in sublist]

            processed_posts += self.process_posts(posts, previews, attachments, page_number, offset, base_server, save_empty_files=self.config.get_empty_posts, id_filter=id_filter)  # Save incremental posts to JSON

        return base_dir, safe_service, safe_user_id, safe_name, processed_posts


class Down:
    def __init__(self, config: Config):
        # Create an SQLite database connection
        self.db = SqliteDatabase('downloaded.db')
        self.model = self.create_model()
        # Connect to the database
        self.db.connect()
        # ensure table creation
        self.db.create_tables([self.model])
        self.config: Config = config

    def create_model(self):
        class DownloadedPosts(Model):
            value = CharField(unique=True)  # A unique string field

            class Meta:
                database = self.db  # Use the 'downloaded.db' SQLite database from the instance

        return DownloadedPosts

    @staticmethod
    def sanitize_filename(filename):
        """Sanitize filename by removing invalid characters and replacing spaces with underscores."""
        filename = re.sub(r'[\\/*?\"<>|]', '', filename)
        return filename.replace(' ', '_')

    def download_file(self, file_url, save_path):
        """Download a file from a URL and save it to the specified path with a progress bar and retry logic."""
        max_retries = 5
        retry_delay = 5  # seconds
        attempt = 0

        while attempt < max_retries:
            try:
                print(i18n.t("download_attempt", test1=attempt + 1, text2=file_url), flush=True)
                response = requests.get(file_url, stream=True, proxies=self.config.get_requests_proxy(), verify=self.config.debug_verify_ssl)
                response.raise_for_status()

                # Get the total file size from headers
                total_size = int(response.headers.get('content-length', 0))

                # Set up the tqdm progress bar
                with open(save_path, 'wb') as f:
                    with tqdm(total=total_size, unit='B', unit_scale=True, unit_divisor=1024, desc="Downloading", leave=False  # Keep the progress bar on the same line
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

                print(i18n.t("download_success2", file_url=file_url), flush=True)
                return  # Exit the function if download is successful

            except Exception as e:
                attempt += 1
                print(f"Warning: Attempt {attempt} failed to download {file_url}: {e}")
                if attempt < max_retries:
                    print(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    print(f"Download failed after {max_retries} attempts.")

    def process_post(self, post, base_folder):
        """Process a single post, downloading its files."""
        post_id = post.get("id")
        post_folder = os.path.join(base_folder, post_id)
        os.makedirs(post_folder, exist_ok=True)

        print(f"Processing post ID {post_id}")

        # Download files using ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as executor:
            for file_url, file_save_path in self.generate_downloads(post, post_folder):
                executor.submit(self.download_file, file_url, file_save_path)

        print(f"Post {post_id} downloaded")

    def generate_downloads(self, post, post_folder):
        for file_index, file in enumerate(post.get("files", []), start=1):
            original_name = file.get("name")
            file_url = file.get("url")
            sanitized_name = self.sanitize_filename(original_name)
            new_filename = f"{file_index}-{sanitized_name}"
            file_save_path = os.path.join(post_folder, new_filename)
            yield file_url, file_save_path

    def run(self, base_folder, post_json):

        # Base folder for posts
        base_folder = os.path.join(base_folder, "posts")
        os.makedirs(base_folder, exist_ok=True)

        # Process each post sequentially

        try:
            self.model.get(self.model.value == post_json.get("id"))
            print(i18n.t("already_downloaded"))

        except Exception:
            print(i18n.t("continue_download"))
        self.process_post(post_json, base_folder)
        self.model.create(value=post_json.get("id"))
        time.sleep(2)  # Wait 2 seconds between posts


class Downloader:
    def __init__(self):
        system_locale, _ = locale.getdefaultlocale()
        language_code = system_locale.split("_")[0]
        i18n.set("locale", language_code)

        i18n.set('filename_format', '{locale}.{format}')
        i18n.set('file_format', 'yaml')
        i18n.set('skip_locale_root_data', True)
        i18n.load_path.append("locales")
        i18n.set("locale", "en")

        # print(i18n.t("bla"))

        self.config = Config.from_yaml("conf.yaml")
        self.down = Down(self.config)
        self.posts = Posts(self.config)

    @staticmethod
    def display_logo():
        print(i18n.t("logo"))

    def run_download_script(self, base_dir, service, user_id, username, json_posts):
        """Roda o script de download com o JSON gerado e faz tracking detalhado em tempo real"""
        try:

            # Análise inicial
            total_posts = len(json_posts)

            # Contagem de arquivos
            total_files = sum(len(post['files']) for post in json_posts)

            # Imprimir informações iniciais
            print(i18n.t("post_extract_complete", total_posts=total_posts))
            print(i18n.t("number_of_files", total_files=total_files))
            print(i18n.t("starting_downloads"))

            # Determinar ordem de processamento

            json_posts = sorted(json_posts, key=lambda x: x['id'], reverse=self.config.process_from_oldest)

            # Base folder for posts using path normalization
            posts_folder = os.path.join(base_dir, service, f"{username} - {user_id}")

            # Processar cada post
            for post in json_posts:
                # Encontrar dados do post específico

                # Pasta do post específico com normalização
                post_folder = os.path.join(posts_folder, "posts", post['id'])

                # Contar número de arquivos no JSON para este post
                expected_files_count = len(post['files'])

                try:

                    self.down.run(posts_folder, post)
                    # Após o download, verificar novamente os arquivos
                    current_files = [f for f in os.listdir(post_folder) if os.path.isfile(os.path.join(post_folder, f))]
                    current_files_count = len(current_files)

                    # Verificar o resultado do download
                    if current_files_count == expected_files_count:
                        print(i18n.t("post_downloaded", post_id=post['id'], current_files_count=current_files_count, expected_files_count=expected_files_count))
                    else:
                        print(i18n.t("post_partially_downloaded", post_id=post['id'], current_files_count=current_files_count, expected_files_count=expected_files_count))

                except Exception as e:
                    print(f"{i18n.t("post_download_error", post_id=post['id'])}: {e}")

                    # Pequeno delay para evitar sobrecarga
                    time.sleep(0.5)

            print(i18n.t("post_all_complete"))

        except Exception as e:
            print(f"Unexpected error: {e}")
            raise e

    def download_profile_posts(self):
        """Option to download posts from a profile"""
        self.display_logo()
        print(i18n.t("download_profile_posts"))

        choice = input(i18n.t("download_profile_posts_choice"))

        if choice == '5':
            return

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

            # Se um JSON foi gerado, roda o script de download
            if json_posts:
                self.run_download_script(base_dir, service, user_id, name, json_posts)
            else:
                print("The JSON path could not be found.")

        except Exception as e:
            raise e

        input("\nPress Enter to continue...")

    def customize_settings(self):
        config: Config = Config.from_yaml("conf.yaml")

        while True:
            self.display_logo()

            print(i18n.t("customize_settings", get_empty_posts=self.config.get_empty_posts, process_from_oldest=self.config.process_from_oldest, save_info=self.config.save_info, post_info=self.config.post_info))
            choice = input(i18n.t("download_profile_posts_choice"))

            if choice == '1':
                config.get_empty_posts = not config.get_empty_posts
            elif choice == '2':
                config.process_from_oldest = config.process_from_oldest
            elif choice == '3':
                config.save_info = not config.save_info
            elif choice == '4':
                # Alternar entre "md" e "txt"
                config.post_info = 'txt' if config.post_info == 'md' else 'md'
            elif choice == '5':
                # Sair do menu de configurações
                break
            else:
                print(i18n.t("invalid_option_try_again"))

            # Salvar as configurações no arquivo
            config.save_to_yaml("conf.yaml")

            print("\nUpdated configurations.")
            time.sleep(1)

    def main_menu(self):
        """Menu principal do aplicativo"""
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
                input(i18n.t("invalid_option"))


if __name__ == "__main__":
    Downloader().main_menu()
