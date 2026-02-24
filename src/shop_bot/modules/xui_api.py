import uuid
from datetime import datetime, timedelta
import logging
from urllib.parse import urlparse
from typing import List, Dict

from py3xui import Api, Client, Inbound

from shop_bot.data_manager.database import (
    get_host,
    get_all_hosts,
    get_key_by_email,
    get_setting,
    get_key_hosts,
    get_all_keys_with_subscription_token,
    add_key_host,
)

logger = logging.getLogger(__name__)

def login_to_host(host_url: str, username: str, password: str, inbound_id: int) -> tuple[Api | None, Inbound | None]:
    try:
        api = Api(host=host_url, username=username, password=password)
        api.login()
        inbounds: List[Inbound] = api.inbound.get_list()
        target_inbound = next((inbound for inbound in inbounds if inbound.id == inbound_id), None)
        
        if target_inbound is None:
            logger.error(f"Inbound с ID '{inbound_id}' не найден на хосте '{host_url}'")
            return None, None
        return api, target_inbound
    except Exception as e:
        logger.error(f"Не удалось выполнить вход или получить inbound для хоста '{host_url}': {e}", exc_info=True)
        return None, None

def get_connection_string(inbound: Inbound, user_uuid: str, host_url: str, remark: str) -> str | None:
    if not inbound: return None
    settings = inbound.stream_settings.reality_settings.get("settings")
    if not settings: return None
    
    public_key = settings.get("publicKey")
    fp = settings.get("fingerprint")
    server_names = inbound.stream_settings.reality_settings.get("serverNames")
    short_ids = inbound.stream_settings.reality_settings.get("shortIds")
    port = inbound.port
    
    if not all([public_key, server_names, short_ids]): return None
    
    parsed_url = urlparse(host_url)
    short_id = short_ids[0]
    
    connection_string = (
        f"vless://{user_uuid}@{parsed_url.hostname}:{port}"
        f"?type=tcp&security=reality&pbk={public_key}&fp={fp}&sni={server_names[0]}"
        f"&sid={short_id}&spx=%2F&flow=xtls-rprx-vision#{remark}"
    )
    return connection_string

def get_subscription_link(user_uuid: str, host_url: str, host_name: str | None = None, sub_token: str | None = None) -> str:
    """Build subscription URL with the following priority:
    1) Host-specific subscription_url (xui_hosts.subscription_url)
    2) Fallback: domain/host_url + default path
    Supports optional token replacement if base contains "{token}".
    """
    host_base = None
    try:
        if host_name:
            host = get_host(host_name)
            if host:
                host_base = (host.get("subscription_url") or "").strip()
    except Exception:
        host_base = None

    base = (host_base or "").strip()

    if sub_token:
        if base:
            return base.replace("{token}", sub_token) if "{token}" in base else f"{base.rstrip('/')}/{sub_token}"
        domain = (get_setting("domain") or "").strip()
        parsed = urlparse(host_url)
        hostname = domain if domain else (parsed.hostname or "")
        scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
        return f"{scheme}://{hostname}/sub/{sub_token}"

    if base:
        return base

    domain = (get_setting("domain") or "").strip()
    parsed = urlparse(host_url)
    hostname = domain if domain else (parsed.hostname or "")
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    return f"{scheme}://{hostname}/sub/{user_uuid}?format=v2ray"

def update_or_create_client_on_panel(api: Api, inbound_id: int, email: str, days_to_add: int | None = None, target_expiry_ms: int | None = None) -> tuple[str | None, int | None, str | None]:
    if inbound_id != 443:
        email = f"{email.split('@')[0]}_{inbound_id}@{email.split('@')[1]}"
    try:
        inbound_to_modify = api.inbound.get_by_id(inbound_id)
        if not inbound_to_modify:
            raise ValueError(f"Could not find inbound with ID {inbound_id}")

        if inbound_to_modify.settings.clients is None:
            inbound_to_modify.settings.clients = []
            
        client_index = -1
        for i, client in enumerate(inbound_to_modify.settings.clients):
            if client.email == email:
                client_index = i
                break
        
        # Determine new expiry time
        if target_expiry_ms is not None:
            new_expiry_ms = int(target_expiry_ms)
        else:
            if days_to_add is None:
                raise ValueError("Either days_to_add or target_expiry_ms must be provided")
            if client_index != -1:
                existing_client = inbound_to_modify.settings.clients[client_index]
                if existing_client.expiry_time > int(datetime.now().timestamp() * 1000):
                    current_expiry_dt = datetime.fromtimestamp(existing_client.expiry_time / 1000)
                    new_expiry_dt = current_expiry_dt + timedelta(days=days_to_add)
                else:
                    new_expiry_dt = datetime.now() + timedelta(days=days_to_add)
            else:
                new_expiry_dt = datetime.now() + timedelta(days=days_to_add)

            new_expiry_ms = int(new_expiry_dt.timestamp() * 1000)

        client_sub_token: str | None = None

        if client_index != -1:
            # Disable auto-reset/auto-renew on extension
            try:
                inbound_to_modify.settings.clients[client_index].reset = 0
            except Exception:
                pass
            inbound_to_modify.settings.clients[client_index].enable = True
            inbound_to_modify.settings.clients[client_index].expiry_time = new_expiry_ms

            existing_client = inbound_to_modify.settings.clients[client_index]
            client_uuid = existing_client.id
            try:
                sub_token_existing = None
                for attr in ("subId", "subscription", "sub_id"):
                    if hasattr(existing_client, attr):
                        val = getattr(existing_client, attr)
                        if val:
                            sub_token_existing = val
                            break
                if sub_token_existing:
                    client_sub_token = sub_token_existing
                else:
                    import secrets
                    client_sub_token = secrets.token_hex(12)
                    for attr in ("subId", "subscription", "sub_id"):
                        try:
                            setattr(existing_client, attr, client_sub_token)
                        except Exception:
                            pass
            except Exception:
                pass
        else:
            client_uuid = str(uuid.uuid4())
            new_client = Client(
                id=client_uuid,
                email=email,
                enable=True,
                flow="xtls-rprx-vision",
                expiry_time=new_expiry_ms
            )
            # Ensure no auto-reset/auto-renew for new clients
            try:
                setattr(new_client, "reset", 0)
            except Exception:
                pass

            try:
                import secrets
                client_sub_token = secrets.token_hex(12)
                for attr in ("subId", "subscription", "sub_id"):
                    try:
                        setattr(new_client, attr, client_sub_token)
                    except Exception:
                        pass
            except Exception:
                pass
            inbound_to_modify.settings.clients.append(new_client)

        api.inbound.update(inbound_id, inbound_to_modify)

        return client_uuid, new_expiry_ms, client_sub_token

    except Exception as e:
        logger.error(f"Ошибка в update_or_create_client_on_panel: {e}", exc_info=True)
        return None, None, None

async def create_or_update_key_on_host(host_name: str, email: str, days_to_add: int | None = None, expiry_timestamp_ms: int | None = None) -> Dict | None:
    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Сбой рабочего процесса: Хост '{host_name}' не найден в базе данных.")
        return None

    api, inbound = login_to_host(
        host_url=host_data['host_url'],
        username=host_data['host_username'],
        password=host_data['host_pass'],
        inbound_id=host_data['host_inbound_id']
    )
    if not api or not inbound:
        logger.error(f"Сбой рабочего процесса: Не удалось войти или найти inbound на хосте '{host_name}'.")
        return None
        
    # Prefer exact expiry when provided (e.g., switching hosts), otherwise add days (purchase/extend/trial)
    client_uuid, new_expiry_ms, client_sub_token = update_or_create_client_on_panel(
        api, inbound.id, email, days_to_add=days_to_add, target_expiry_ms=expiry_timestamp_ms
    )

    if not client_uuid:
        logger.error(f"Сбой рабочего процесса: Не удалось создать/обновить клиента '{email}' на хосте '{host_name}'.")
        return None
    
    connection_string = get_subscription_link(client_uuid, host_data['host_url'], host_name, sub_token=client_sub_token)
    
    logger.info(f"Успешно обработан ключ для '{email}' на хосте '{host_name}'.")
    
    
    return {
        "client_uuid": client_uuid,
        "email": email,
        "expiry_timestamp_ms": new_expiry_ms,
        "connection_string": connection_string,
        "host_name": host_name
    }

async def get_key_details_from_host(key_data: dict) -> dict | None:
    # Единый ключ (все хосты): отдаём одну ссылку на подписку-агрегатор
    if key_data.get("subscription_token"):
        return {"connection_string": get_unified_subscription_link(key_data["subscription_token"])}

    host_name = key_data.get('host_name')
    if not host_name:
        logger.error(f"Не удалось получить данные ключа: отсутствует host_name для key_id {key_data.get('key_id')}")
        return None

    host_db_data = get_host(host_name)
    if not host_db_data:
        logger.error(f"Не удалось получить данные ключа: хост '{host_name}' не найден в базе данных.")
        return None

    api, inbound = login_to_host(
        host_url=host_db_data['host_url'],
        username=host_db_data['host_username'],
        password=host_db_data['host_pass'],
        inbound_id=host_db_data['host_inbound_id']
    )
    if not api or not inbound: return None

    client_sub_token = None
    try:
        if inbound.settings and inbound.settings.clients:
            for client in inbound.settings.clients:
                if getattr(client, "id", None) == key_data['xui_client_uuid'] or getattr(client, "email", None) == key_data.get('email'):
                    candidate_fields = ("subId", "subscription", "sub_id", "subscriptionId", "subscription_token")
                    for attr in candidate_fields:
                        val = None
                        if hasattr(client, attr):
                            val = getattr(client, attr)
                        else:
                            try:
                                val = client.get(attr)
                            except Exception:
                                pass
                        if val:
                            client_sub_token = val
                            break
                    break
    except Exception:
        pass
    connection_string = get_subscription_link(key_data['xui_client_uuid'], host_db_data['host_url'], host_name, sub_token=client_sub_token)
    return {"connection_string": connection_string}

def get_unified_subscription_link(subscription_token: str) -> str:
    """URL единой подписки (все хосты в одном ключе) — указывает на наш агрегатор."""
    domain = (get_setting("domain") or "").strip()
    if not domain:
        return ""
    scheme = "https"
    return f"{scheme}://{domain}/sub/{subscription_token}"

async def create_or_update_key_on_all_hosts(
    base_email: str,
    days_to_add: int | None = None,
    expiry_timestamp_ms: int | None = None,
) -> List[Dict]:
    """
    Создать/обновить клиента на всех хостах. Возвращает список результатов по каждому хосту.
    Каждый элемент: { host_name, client_uuid, email, expiry_timestamp_ms }.
    """
    hosts = get_all_hosts()
    if not hosts:
        return []
    results = []
    for host in hosts:
        host_name = host.get("host_name")
        if not host_name:
            continue
        r = await create_or_update_key_on_host(
            host_name=host_name,
            email=base_email,
            days_to_add=days_to_add,
            expiry_timestamp_ms=expiry_timestamp_ms,
        )
        if r:
            results.append(r)
    return results

def get_connection_string_for_client(host_name: str, client_uuid: str, key_email: str, remark: str | None = None) -> str | None:
    """
    Получить VLESS connection string для клиента на указанном хосте (для агрегатора подписки).
    """
    host_data = get_host(host_name)
    if not host_data:
        return None
    api, inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
    )
    if not api or not inbound or not inbound.settings or not inbound.settings.clients:
        return None
    for client in inbound.settings.clients:
        cid = getattr(client, "id", None)
        cemail = getattr(client, "email", None)
        if cid == client_uuid or cemail == key_email:
            remark = remark or (host_name or "server")
            return get_connection_string(inbound, cid, host_data["host_url"], remark)
    return None

async def get_aggregated_connection_strings(key_id: int) -> List[str]:
    """
    Собрать все VLESS-строки для ключа по всем key_hosts (для отдачи в /sub/<token>).
    """
    from urllib.parse import urlparse
    hosts_list = get_key_hosts(key_id)
    if not hosts_list:
        return []
    out = []
    for kh in hosts_list:
        host_name = kh.get("host_name")
        client_uuid = kh.get("xui_client_uuid")
        key_email = kh.get("key_email")
        if not host_name or not client_uuid:
            continue
        remark = (host_name or "server").replace(" ", "-")
        cs = get_connection_string_for_client(host_name, client_uuid, key_email, remark=remark)
        if cs:
            out.append(cs)
    return out

async def sync_keys_to_new_host(new_host_name: str) -> int:
    """
    Синхронизация: для каждого ключа с единой подпиской создать клиента на новом хосте
    и добавить запись в key_hosts. Возвращает количество обновлённых ключей.
    """
    from datetime import datetime
    keys = get_all_keys_with_subscription_token()
    synced = 0
    for key in keys:
        try:
            key_id = key["key_id"]
            base_email = key.get("key_email")
            if not base_email:
                continue
            expiry = key.get("expiry_date")
            if expiry:
                if isinstance(expiry, str):
                    expiry_dt = datetime.fromisoformat(expiry.replace("Z", ""))
                else:
                    expiry_dt = expiry
                expiry_ms = int(expiry_dt.timestamp() * 1000)
            else:
                expiry_ms = int(datetime.now().timestamp() * 1000) + 30 * 24 * 3600 * 1000
            r = await create_or_update_key_on_host(
                host_name=new_host_name,
                email=base_email,
                expiry_timestamp_ms=expiry_ms,
            )
            if r:
                add_key_host(key_id, new_host_name, r["client_uuid"], r["email"])
                synced += 1
        except Exception as e:
            logger.warning(f"sync_keys_to_new_host key_id={key.get('key_id')}: {e}")
    return synced

async def delete_client_on_host(host_name: str, client_email: str) -> bool:
    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Не удалось удалить клиента: хост '{host_name}' не найден.")
        return False

    api, inbound = login_to_host(
        host_url=host_data['host_url'],
        username=host_data['host_username'],
        password=host_data['host_pass'],
        inbound_id=host_data['host_inbound_id']
    )

    if not api or not inbound:
        logger.error(f"Не удалось удалить клиента: ошибка входа или поиска inbound для хоста '{host_name}'.")
        return False
        
    try:
        client_to_delete = get_key_by_email(client_email)
        if client_to_delete:
            api.client.delete(inbound.id, client_to_delete['xui_client_uuid'])
            logger.info(f"Клиент '{client_email}' успешно удалён с хоста '{host_name}'.")
            return True
        else:
            logger.warning(f"Клиент с email '{client_email}' не найден на хосте '{host_name}' для удаления (возможно, уже удалён).")
            return True
            
    except Exception as e:
        logger.error(f"Не удалось удалить клиента '{client_email}' с хоста '{host_name}': {e}", exc_info=True)
        return False