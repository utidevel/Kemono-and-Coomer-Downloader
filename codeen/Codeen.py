import importlib
import json
import os
import re
import subprocess
import sys
import time
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
import yaml
from peewee import SqliteDatabase, Model, CharField
from pydantic import create_model
from tqdm import tqdm


class Config:

    def __init__(self, proxy_username: str, proxy_password: str, proxy_url: str, save_info:bool, post_info:str, process_from_oldest:bool, get_empty_posts:bool, debug_verify_ssl:bool):
        self.proxy_username: str = proxy_username
        self.proxy_password: str = proxy_password
        self.proxy_url: str = proxy_url
        self.save_info: bool = save_info
        self.post_info: str = post_info
        self.process_from_oldest: bool = process_from_oldest
        self.get_empty_posts: bool = get_empty_posts
        self.debug_verify_ssl: bool = debug_verify_ssl

    @classmethod
    def from_yaml(cls, file_path: str) -> "Config":
        """
        Loads configuration from a YAML file and returns a Config instance.

        :param file_path: Path to the YAML file.
        :return: An instance of Config.
        """
        try:
            with open(file_path, 'r') as file:
                data = yaml.safe_load(file)
            return cls(
                proxy_username=data.get('proxy_username', None),
                proxy_password=data.get('proxy_password', None),
                proxy_url=data.get('proxy_url', False),
                save_info=data.get('save_info', False),
                post_info=data.get('post_info', False),
                process_from_oldest=data.get('process_from_oldest', False),
                get_empty_posts=data.get('get_empty_posts', False),
                debug_verify_ssl=data.get('debug_verify_ssl', True)
            )
        except FileNotFoundError:
            print(f"Error: The file '{file_path}' was not found.")
            raise Exception("Configuration not found")
        except yaml.YAMLError as e:
            raise Exception("Configuration has errors")

    def save_to_yaml(self, file_path):
        """
        Saves the current configuration back to the YAML file.

        :param file_path: Path to the YAML file to save the data.
        """
        data = {
            'proxy_username': self.proxy_username,
            'proxy_password': self.proxy_password,
            'proxy_url': self.proxy_url,
            'save_info': self.save_info,
            'post_info': self.post_info,
            'process_from_oldest': self.process_from_oldest,
            'get_empty_posts': self.get_empty_posts,
        }
        try:
            with open(file_path, 'w') as file:
                yaml.dump(data, file, default_flow_style=False)
            print(f"Configuration successfully saved to {file_path}")
        except Exception as e:
            print(f"Error saving configuration to {file_path}: {e}")

    def __str__(self):
        return (f"ProxyConfig("
                f"proxy_username='{self.proxy_username}', "
                f"proxy_password='{'*' * len(self.proxy_password)}', "  # Mask the password
                f"proxy_url='{self.proxy_url}', "
                f"save_info={self.save_info}, "
                f"post_info={self.post_info}, "
                f"process_from_oldest={self.process_from_oldest}, "
                f"get_empty_posts={self.get_empty_posts})")

    def get_requests_proxy(self):
        if self.proxy_url is not None and self.proxy_url.strip() != '':
            return {'http': f"http://{self.proxy_username}:{self.proxy_password}@{self.proxy_url}", 'https': f"http://s{self.proxy_username}:{self.proxy_password}@{self.proxy_url}"}
        else:
            return None


def install_requirements():
    """Verifica e instala as dependências do requirements.txt."""
    requirements_file = "requirements.txt"

    if not os.path.exists(requirements_file):
        print(f"Error: File {requirements_file} not found.")
        return

    with open(requirements_file, 'r', encoding='utf-8') as req_file:
        for line in req_file:
            # Lê cada linha, ignora vazias ou comentários
            package = line.strip()
            if package and not package.startswith("#"):
                try:
                    # Tenta importar o pacote para verificar se já está instalado
                    package_name = package.split("==")[0]  # Ignora versão específica na importação
                    importlib.import_module(package_name)
                except ImportError:
                    # Se falhar, instala o pacote usando pip
                    print(f"Installing the package: {package}")
                    subprocess.check_call([sys.executable, "-m", "pip", "install", package])


class Posts:

    def __init__(self, config: Config):
        self.config: Config = config

    def save_json(self,file_path, data):
        """Helper function to save JSON files with UTF-8 encoding and pretty formatting"""
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def get_base_config(self,profile_url):
        """
        Dynamically configure base URLs and directories based on the profile URL domain
        """
        # Extract domain from the profile URL
        domain = profile_url.split('/')[2]

        if domain not in ['kemono.su', 'coomer.su']:
            raise ValueError(f"Unsupported domain: {domain}")

        BASE_API_URL = f"https://{domain}/api/v1"
        BASE_SERVER = f"https://{domain}"
        BASE_DIR = domain.split('.')[0]  # 'kemono' or 'coomer'

        return BASE_API_URL, BASE_SERVER, BASE_DIR

    def is_offset(self,value):
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

        raise ValueError(f"Modo de busca inválido: {fetch_mode}")

    def get_artist_info(self,profile_url):
        # Extrair serviço e user_id do URL
        parts = profile_url.split("/")
        service = parts[-3]
        user_id = parts[-1]
        return service, user_id

    def fetch_posts(self,base_api_url, service, user_id, offset=0):
        # Buscar posts da API
        url = f"{base_api_url}/{service}/user/{user_id}/posts-legacy?o={offset}"
        response = requests.get(url,proxies=self.config.get_requests_proxy(), verify=self.config.debug_verify_ssl)
        response.raise_for_status()
        return response.json()

    def save_json_incrementally(self,file_path, new_posts, start_offset, end_offset):
        # Criar um novo dicionário com os posts atuais
        data = {
            "total_posts": len(new_posts),
            "posts": new_posts
        }

        # Salvar o novo arquivo, substituindo o existente
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def process_posts(self,posts, previews, attachments_data, page_number, offset, base_server, save_empty_files=True,
                      id_filter=None):
        # Processar posts e organizar os links dos arquivos
        processed = []
        for post in posts:
            # Filtro de ID se especificado
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

            # Combina previews e attachments_data em uma única lista para busca
            all_data = previews + attachments_data

            # Processar arquivos no campo file
            if "file" in post and post["file"]:
                matching_data = next(
                    (item for item in all_data if item["path"] == post["file"]["path"]),
                    None
                )
                if matching_data:
                    file_url = f"{matching_data['server']}/data{post['file']['path']}"
                    if file_url not in [f["url"] for f in result["files"]]:
                        result["files"].append({"name": post["file"]["name"], "url": file_url})

            # Processar arquivos no campo attachments
            for attachment in post.get("attachments", []):
                matching_data = next(
                    (item for item in all_data if item["path"] == attachment["path"]),
                    None
                )
                if matching_data:
                    file_url = f"{matching_data['server']}/data{attachment['path']}"
                    if file_url not in [f["url"] for f in result["files"]]:
                        result["files"].append({"name": attachment["name"], "url": file_url})

            # Ignorar posts sem arquivos se save_empty_files for False
            if not save_empty_files and not result["files"]:
                continue

            processed.append(result)

        return processed

    def sanitize_filename(self,value):
        """Remove caracteres que podem quebrar a criação de pastas."""
        return value.replace("/", "_").replace("\\", "_")

    def run(self,profile_url: str, fetch_mode: str):

        # Pegar o valor de 'process_from_oldest' da configuração
        SAVE_EMPTY_FILES = self.config.get_empty_posts  # Alterar para True se quiser salvar posts sem arquivos

        # Configurar base URLs dinamicamente
        BASE_API_URL, BASE_SERVER, BASE_DIR = self.get_base_config(profile_url)

        # Pasta base
        base_dir = BASE_DIR
        os.makedirs(base_dir, exist_ok=True)

        # Atualizar o arquivo profiles.json
        profiles_file = os.path.join(base_dir, "profiles.json")
        if os.path.exists(profiles_file):
            with open(profiles_file, "r", encoding="utf-8") as f:
                profiles = json.load(f)
        else:
            profiles = {}

        # Buscar primeiro conjunto de posts para informações gerais
        service, user_id = self.get_artist_info(profile_url)
        initial_data = self.fetch_posts(BASE_API_URL, service, user_id, offset=0)
        name = initial_data["props"]["name"]
        count = initial_data["props"]["count"]

        # Salvar informações do artista
        artist_info = {
            "id": user_id,
            "name": name,
            "service": service,
            "indexed": initial_data["props"]["artist"]["indexed"],
            "updated": initial_data["props"]["artist"]["updated"],
            "public_id": initial_data["props"]["artist"]["public_id"],
            "relation_id": initial_data["props"]["artist"]["relation_id"],
        }
        profiles[user_id] = artist_info
        self.save_json(profiles_file, profiles)

        # Sanitizar os valores
        safe_name = self.sanitize_filename(name)
        safe_service = self.sanitize_filename(service)
        safe_user_id = self.sanitize_filename(user_id)

        # Pasta do artista
        artist_dir = os.path.join(base_dir, f"{safe_name}-{safe_service}-{safe_user_id}")
        os.makedirs(artist_dir, exist_ok=True)

        # Processar modo de busca
        today = datetime.now().strftime("%Y-%m-%d")

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

        # Nome do arquivo JSON com range de offsets
        if len(offsets) > 1:
            file_path = os.path.join(artist_dir, f"posts-{offsets[0]}-{offsets[-1]}-{today}.json")
        else:
            file_path = os.path.join(artist_dir, f"posts-{offsets[0]}-{today}.json")

        new_posts = []
        # Processamento principal
        for offset in offsets:
            page_number = (offset // 50) + 1
            post_data = self.fetch_posts(BASE_API_URL, service, user_id, offset=offset)
            posts = post_data["results"]
            previews = [item for sublist in post_data.get("result_previews", []) for item in sublist]
            attachments = [item for sublist in post_data.get("result_attachments", []) for item in sublist]

            processed_posts = self.process_posts(
                posts,
                previews,
                attachments,
                page_number,
                offset,
                BASE_SERVER,
                save_empty_files=SAVE_EMPTY_FILES,
                id_filter=id_filter
            )
            new_posts.extend(processed_posts)
            # Salvar posts incrementais no JSON
            if processed_posts:
                self.save_json_incrementally(file_path, new_posts, offset, offset + 50)

                # Verificar se encontrou os IDs desejados
                if id_filter:
                    found_ids.update(post['id'] for post in processed_posts)

                    # Verificar se encontrou ambos os IDs
                    if (id1 in found_ids) and (id2 in found_ids):
                        print(f"Found both IDs: {id1} e {id2}")
                        break

        # Imprimir o caminho completo do arquivo JSON gerado
        print(f"{os.path.abspath(file_path)}")
        return f"{os.path.abspath(file_path)}"

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

    def sanitize_filename(self, filename):
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
                print(f"Attempt {attempt + 1} to download {file_url}", flush=True)
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
                    print(f"\nDownload {file_url} success. File size is correct: {downloaded_size} bytes.")
                else:
                    print(f"\nDownload {file_url} incomplete. Expected {total_size} bytes but got {downloaded_size} bytes.")
                    raise Exception("not complete try again")

                print(f"\nDownload {file_url} success", flush=True)
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

        # Prepare downloads for this post
        downloads = []
        for file_index, file in enumerate(post.get("files", []), start=1):
            original_name = file.get("name")
            file_url = file.get("url")
            sanitized_name = self.sanitize_filename(original_name)
            new_filename = f"{file_index}-{sanitized_name}"
            file_save_path = os.path.join(post_folder, new_filename)
            downloads.append((file_url, file_save_path))

        # Download files using ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as executor:
            for file_url, file_save_path in downloads:
                executor.submit(self.download_file, file_url, file_save_path)

        print(f"Post {post_id} downloaded")

    def run(self, json_file_path: str):
        # Verifica se o arquivo existe
        if not os.path.exists(json_file_path):
            print(f"Error: The file '{json_file_path}' was not found.")
            sys.exit(1)

        # Load the JSON file
        with open(json_file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Base folder for posts
        base_folder = os.path.join(os.path.dirname(json_file_path), "posts")
        os.makedirs(base_folder, exist_ok=True)

        # Caminho para o arquivo de configuração
        config_file_path = os.path.join("config", "conf.json")

        posts = data.get("posts", [])
        if self.config.process_from_oldest:
            posts = reversed(posts)

        # Process each post sequentially
        for post_index, post in enumerate(posts, start=1):
            try:
                self.model.get(self.model.value == post.get("id"))
                print("Already Downloaded Skipping")
                continue
            except Exception:
                print("The link was not found in the database. Contime download")
            self.process_post(post, base_folder)
            self.model.create(value=post.get("id"))
            time.sleep(2)  # Wait 2 seconds between posts


class Codeen:
    down = None

    def __init__(self):
        self.config = Config.from_yaml("conf.yaml")
        self.down = Down(self.config)
        self.posts = Posts(self.config)

    def clear_screen(self):
        """Limpa a tela do console de forma compatível com diferentes sistemas operacionais"""
        os.system('self' if os.name == 'nt' else 'clear')

    def display_logo(self):
        """Exibe o logo do projeto"""
        logo = """
     _  __                                                   
    | |/ /___ _ __ ___   ___  _ __   ___                     
    | ' // _ \ '_ ` _ \ / _ \| '_ \ / _ \                    
    | . \  __/ | | | | | (_) | | | | (_) |                   
    |_|\_\___|_| |_| |_|\___/|_| |_|\___/                    
     / ___|___   ___  _ __ ___   ___ _ __                    
    | |   / _ \ / _ \| '_ ` _ \ / _ \ '__|                   
    | |__| (_) | (_) | | | | | |  __/ |                      
     \____\___/ \___/|_| |_| |_|\___|_|          _           
    |  _ \  _____      ___ __ | | ___   __ _  __| | ___ _ __ 
    | | | |/ _ \ \ /\ / / '_ \| |/ _ \ / _` |/ _` |/ _ \ '__|
    | |_| | (_) \ V  V /| | | | | (_) | (_| | (_| |  __/ |   
    |____/ \___/ \_/\_/ |_| |_|_|\___/ \__,_|\__,_|\___|_|   
    
    Created by E43b
    GitHub: https://github.com/e43b
    GitHub: https://github.com/e43b
    Discord: https://discord.gg/GNJbxzD8bK
    Project Repository: https://github.com/e43b/Kemono-and-Coomer-Downloader
    Donate: https://ko-fi.com/e43bs
    """
        print(logo)

    def normalize_path(self, path):
        """
        Normaliza o caminho do arquivo para lidar com caracteres não-ASCII
        """
        try:
            # Se o caminho original existir, retorna ele
            if os.path.exists(path):
                return path

            # Extrai o nome do arquivo e os componentes do caminho
            filename = os.path.basename(path)
            path_parts = path.split(os.sep)

            # Identifica se está procurando em kemono ou coomer
            base_dir = None
            if 'kemono' in path_parts:
                base_dir = 'kemono'
            elif 'coomer' in path_parts:
                base_dir = 'coomer'

            if base_dir:
                # Procura em todos os subdiretórios do diretório base
                for root, dirs, files in os.walk(base_dir):
                    if filename in files:
                        return os.path.join(root, filename)

            # Se ainda não encontrou, tenta o caminho normalizado
            return os.path.abspath(os.path.normpath(path))

        except Exception as e:
            print(f"Error when normalizing path: {e}")
            return path

    def run_download_script(self, json_path):
        """Roda o script de download com o JSON gerado e faz tracking detalhado em tempo real"""
        try:
            # Normalizar o caminho do JSON
            json_path = self.normalize_path(json_path)

            # Verificar se o arquivo JSON existe
            if not os.path.exists(json_path):
                print(f"Error: JSON file not found: {json_path}")
                return

            # Ler o JSON de posts
            with open(json_path, 'r', encoding='utf-8') as posts_file:
                posts_data = json.load(posts_file)

            # Análise inicial
            total_posts = posts_data['total_posts']
            post_ids = [post['id'] for post in posts_data['posts']]

            # Contagem de arquivos
            total_files = sum(len(post['files']) for post in posts_data['posts'])

            # Imprimir informações iniciais
            print(f"Post extraction completed: {total_posts} posts found")
            print(f"Total number of files to download: {total_files}")
            print("Starting post downloads")

            # Determinar ordem de processamento
            if self.config.process_from_oldest:
                post_ids = sorted(post_ids)  # Ordem do mais antigo ao mais recente
            else:
                post_ids = sorted(post_ids, reverse=True)  # Ordem do mais recente ao mais antigo

            # Pasta base para posts usando normalização de caminho
            posts_folder = self.normalize_path(os.path.join(os.path.dirname(json_path), 'posts'))
            os.makedirs(posts_folder, exist_ok=True)

            # Processar cada post
            for idx, post_id in enumerate(post_ids, 1):
                # Encontrar dados do post específico
                post_data = next((p for p in posts_data['posts'] if p['id'] == post_id), None)

                if post_data:
                    # Pasta do post específico com normalização
                    post_folder = self.normalize_path(os.path.join(posts_folder, post_id))
                    os.makedirs(post_folder, exist_ok=True)

                    # Contar número de arquivos no JSON para este post
                    expected_files_count = len(post_data['files'])

                    # Contar arquivos já existentes na pasta
                    existing_files = [f for f in os.listdir(post_folder) if os.path.isfile(os.path.join(post_folder, f))]
                    existing_files_count = len(existing_files)

                    # Se já tem todos os arquivos, pula o download
                    if existing_files_count == expected_files_count:
                        continue

                    try:

                        self.down.run(json_path)

                        # Após o download, verificar novamente os arquivos
                        current_files = [f for f in os.listdir(post_folder) if os.path.isfile(os.path.join(post_folder, f))]
                        current_files_count = len(current_files)

                        # Verificar o resultado do download
                        if current_files_count == expected_files_count:
                            print(f"Post {post_id} downloaded completely ({current_files_count}/{expected_files_count} files)")
                        else:
                            print(f"Post {post_id} partially downloaded: {current_files_count}/{expected_files_count} files")

                    except Exception as e:
                        print(f"Error while downloading post {post_id}: {e}")

                    # Pequeno delay para evitar sobrecarga
                    time.sleep(0.5)

            print("\nAll posts have been processed!")

        except Exception as e:
            print(f"Unexpected error: {e}")
            # Adicionar mais detalhes para diagnóstico
            import traceback
            traceback.print_exc()

    def download_specific_posts(self):
        """Opção para baixar posts específicos"""
        self.clear_screen()
        self.display_logo()
        print("Download 1 post or a few separate posts")
        print("------------------------------------")
        print("Choose the input method:")
        print("1 - Enter the links directly")
        print("2 - Loading links from a TXT file")
        print("3 - Back to the main menu")
        choice = input("\nEnter your choice (1/2/3): ")

        links = []

        if choice == '3':
            return

        elif choice == '1':
            print("Paste the links to the posts (separated by commas):")
            links = input("Links: ").split(',')
        elif choice == '2':
            file_path = input("Enter the path to the TXT file: ").strip()
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as file:
                    content = file.read()
                    links = content.split(',')
            else:
                print(f"Error: The file '{file_path}' was not found.")
                input("\nPress Enter to continue...")
                return
        else:
            print("Invalid option. Return to the previous menu.")
            input("\nPress Enter to continue...")
            return

        links = [link.strip() for link in links if link.strip()]

        for link in links:
            try:
                domain = link.split('/')[2]
                if domain == 'kemono.su':
                    import codes.kcposts as kcposts
                    kcposts.run(link)
                elif domain == 'coomer.su':
                    import codes.kcposts as kcposts
                    kcposts.run(link)
                else:
                    print(f"Domain not supported: {domain}")
                    continue
            except Exception as e:
                print(f"There has been an error downlaoding {link} - {e}")

        input("\nPress Enter to continue...")

    def download_profile_posts(self):
        """Opção para baixar posts de um perfil"""
        self.clear_screen()
        self.display_logo()
        print("Download Profile Posts")
        print("-----------------------")
        print("1 - Download all posts from a profile")
        print("2 - Download posts from a specific page")
        print("3 - Downloading posts from a range of pages")
        print("4 - Downloading posts between two specific posts")
        print("5 - Back to the main menu")

        choice = input("\nEnter your choice (1/2/3/4/5): ")

        if choice == '5':
            return

        profile_link = input("Paste the profile link: ")

        try:
            json_path = None

            if choice == '1':
                json_path = self.posts.run(profile_link, 'all')

            elif choice == '2':
                page = input("Enter the page number (0 = first page, 50 = second, etc.): ")
                json_path = self.posts.run(profile_link, page)

            elif choice == '3':
                start_page = input("Enter the start page (start, 0, 50, 100, etc.): ")
                end_page = input("Enter the final page (or use end, 300, 350, 400): ")
                json_path = self.posts.run(profile_link, f"{start_page}-{end_page}")

            elif choice == '4':
                first_post = input("Paste the link or ID of the first post: ")
                second_post = input("Paste the link or ID from the second post: ")

                first_id = first_post.split('/')[-1] if '/' in first_post else first_post
                second_id = second_post.split('/')[-1] if '/' in second_post else second_post

                json_path =self.posts.run(profile_link, f"{first_id}-{second_id}")

            # Se um JSON foi gerado, roda o script de download
            if json_path:
                self.run_download_script(json_path)
            else:
                print("The JSON path could not be found.")

        except Exception as e:
            raise e

        input("\nPress Enter to continue...")

    def customize_settings(self):
        """Opção para personalizar configurações"""
        config_path = os.path.join('config', 'conf.json')

        config: Config = Config.from_yaml("conf.yaml")

        while True:
            self.clear_screen()
            self.display_logo()
            print("Customize Settings")
            print("------------------------")
            print(f"1 - Take empty posts: {config['get_empty_posts']}")
            print(f"2 - Download older posts first: {config['process_from_oldest']}")
            print(f"3 - For individual posts, create a file with information (title, description, etc.): {config['save_info']}")
            print(f"4 - Choose the type of file to save the information (Markdown or TXT): {config['post_info']}")
            print("5 - Back to the main menu")

            choice = input("\nChoose an option (1/2/3/4/5): ")

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
                print("Invalid option. Please try again.")

            # Salvar as configurações no arquivo
            config.save_to_yaml("conf.yaml")

            print("\nUpdated configurations.")
            time.sleep(1)

    def main_menu(self):
        """Menu principal do aplicativo"""
        while True:
            self.clear_screen()
            self.display_logo()
            print("Choose an option:")
            print("1 - Download 1 post or a few separate posts")
            print("2 - Download all posts from a profile")
            print("3 - Customize the program settings")
            print("4 - remove database")
            print("5 - Exit the program")

            choice = input("\nEnter your choice (1/2/3/4): ")

            if choice == '1':
                self.download_specific_posts()
            elif choice == '2':
                self.download_profile_posts()
            elif choice == '3':
                self.customize_settings()
            elif choice == '4':
                os.remove("downloaded.db")
            elif choice == '5':
                print("Leaving the program. See you later!")
                break
            else:
                input("Invalid option. Press Enter to continue...")


if __name__ == "__main__":
    print("Checking dependencies...")
    install_requirements()
    print("Verified dependencies.\n")
    Codeen().main_menu()
